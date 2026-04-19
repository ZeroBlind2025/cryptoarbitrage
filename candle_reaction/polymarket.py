"""Minimal Polymarket client for the BTC 5m updown market.

Scope is deliberately narrow:

  * ``discover_btc_updown_5m()`` -- given a unix timestamp, locate the
    Polymarket ``bitcoin-updown-5m-<resolve_ts>`` event and return the
    YES-UP and YES-DOWN CLOB token ids, the resolve timestamp, and
    the slug. No coupling to ``hf_engine``.
  * ``get_top_of_book(token_id)`` -- CLOB order-book call, returns
    ``(bid, ask, bid_size, ask_size)``. Ask is what a paper BUY would
    pay at the top of book; if either side is empty, returns ``None``
    for that field.

Both endpoints are publicly readable, no auth / signing required.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Optional

import requests

log = logging.getLogger("candle_reaction.polymarket")

GAMMA_BASE = "https://gamma-api.polymarket.com"
CLOB_BASE = "https://clob.polymarket.com"

# Slug root candidates (Polymarket ships both short and long forms).
_SLUG_ROOTS = ("btc-updown-5m", "bitcoin-updown-5m")

WINDOW_SEC = 300  # 5m


@dataclass(frozen=True)
class PolyMarket:
    slug: str
    condition_id: str
    yes_up_token_id: str
    yes_down_token_id: str
    resolve_ts: int  # unix seconds the market settles


@dataclass(frozen=True)
class TopOfBook:
    bid: Optional[float]
    ask: Optional[float]
    bid_size: Optional[float]
    ask_size: Optional[float]


def _bucket_ts(now_ts: int, offset_windows: int = 0) -> int:
    """Return the start of the 5m bucket shifted by ``offset_windows``."""
    base = (now_ts // WINDOW_SEC) * WINDOW_SEC
    return base + offset_windows * WINDOW_SEC


def discover_btc_updown_5m(
    now_ts: int,
    session: Optional[requests.Session] = None,
) -> Optional[PolyMarket]:
    """Find the BTC 5m updown market that resolves at the *next* 5m close.

    Polymarket names these markets by the settlement timestamp
    (e.g. ``bitcoin-updown-5m-1772397000``). We try the next bucket
    first, then the current bucket as a fallback for edge-of-interval
    cases.
    """
    sess = session or requests.Session()

    candidates: list[str] = []
    for root in _SLUG_ROOTS:
        for offset in (1, 0, 2):  # next bucket, current, two-ahead
            candidates.append(f"{root}-{_bucket_ts(now_ts, offset)}")

    for slug in candidates:
        market = _fetch_event(sess, slug)
        if market is not None:
            return _reconcile_via_clob(market, session=sess)
    return None


def _fetch_event(sess: requests.Session, slug: str) -> Optional[PolyMarket]:
    try:
        r = sess.get(f"{GAMMA_BASE}/events", params={"slug": slug}, timeout=6)
    except requests.RequestException as e:
        log.debug("gamma slug %s network error: %s", slug, e)
        return None
    if r.status_code != 200:
        return None
    try:
        data = r.json()
    except ValueError:
        return None

    events = data if isinstance(data, list) else [data] if isinstance(data, dict) else []
    for ev in events:
        for mkt in ev.get("markets", []) or []:
            parsed = _parse_market(mkt, slug)
            if parsed is not None:
                return parsed
    return None


def _parse_market(mkt: dict, slug: str) -> Optional[PolyMarket]:
    """Pull token ids + resolve ts out of a Gamma market dict."""
    token_ids = _decode_list(mkt.get("clobTokenIds") or mkt.get("tokens"))
    if not token_ids or len(token_ids) < 2:
        return None

    # Gamma's outcomes[] array is the nominal label source, but its
    # order relative to clobTokenIds is not always reliable. We do a
    # best-guess here; the caller (discover_*) will override this
    # mapping with CLOB's authoritative token->outcome lookup.
    outcomes = _decode_list(mkt.get("outcomes"))
    up_idx = _up_outcome_index(outcomes)
    down_idx = 1 - up_idx

    yes_up_id = str(token_ids[up_idx])
    yes_down_id = str(token_ids[down_idx])

    condition_id = str(mkt.get("conditionId") or mkt.get("id") or "")

    resolve_ts = _parse_resolve_ts(mkt, slug)
    if resolve_ts is None:
        return None

    return PolyMarket(
        slug=slug,
        condition_id=condition_id,
        yes_up_token_id=yes_up_id,
        yes_down_token_id=yes_down_id,
        resolve_ts=resolve_ts,
    )


def _clob_token_outcome_map(
    condition_id: str,
    session: Optional[requests.Session] = None,
) -> Optional[dict[str, str]]:
    """Return ``{"up": token_id, "down": token_id}`` from CLOB.

    CLOB ``/markets/{cid}`` reports each token with its authoritative
    ``outcome`` label. This is the mapping used for resolution, so we
    key entry off the same source -- any mismatch against Gamma's
    inferred order is resolved in CLOB's favour.
    """
    if not condition_id:
        return None
    sess = session or requests.Session()
    try:
        r = sess.get(f"{CLOB_BASE}/markets/{condition_id}", timeout=6)
    except requests.RequestException:
        return None
    if r.status_code != 200:
        return None
    try:
        data = r.json()
    except ValueError:
        return None
    mapping: dict[str, str] = {}
    for t in (data.get("tokens") or []):
        outcome = (t.get("outcome") or "").strip().lower()
        tid = str(t.get("token_id", "")).strip()
        if not tid:
            continue
        if outcome in _UP_WORDS:
            mapping["up"] = tid
        elif outcome in {"down", "no", "lower", "below"}:
            mapping["down"] = tid
    if "up" in mapping and "down" in mapping:
        return mapping
    return None


def _reconcile_via_clob(
    market: "PolyMarket",
    session: Optional[requests.Session] = None,
) -> "PolyMarket":
    """Override Gamma-inferred token ids with CLOB's authoritative map.

    Returns the market unchanged if CLOB is unreachable, so discovery
    degrades gracefully to the Gamma-only path. When CLOB responds,
    the yes_up/yes_down mapping becomes identical to what the
    resolver reads -- no possibility of inversion.
    """
    m = _clob_token_outcome_map(market.condition_id, session=session)
    if not m:
        return market
    if (m["up"] == market.yes_up_token_id
            and m["down"] == market.yes_down_token_id):
        return market
    log.info("reconciled token order via CLOB for %s (Gamma had it swapped)",
             market.slug)
    return PolyMarket(
        slug=market.slug,
        condition_id=market.condition_id,
        yes_up_token_id=m["up"],
        yes_down_token_id=m["down"],
        resolve_ts=market.resolve_ts,
    )


def _decode_list(value):
    """Gamma often ships list fields as JSON-encoded strings; normalise."""
    if isinstance(value, str):
        import json
        try:
            value = json.loads(value)
        except ValueError:
            return None
    if isinstance(value, (list, tuple)):
        return list(value)
    return None


_UP_WORDS = {"up", "yes", "higher", "above"}


def _up_outcome_index(outcomes) -> int:
    if not outcomes:
        return 0
    for i, name in enumerate(outcomes[:2]):
        if isinstance(name, str) and name.strip().lower() in _UP_WORDS:
            return i
    return 0


def _parse_resolve_ts(mkt: dict, slug: str) -> Optional[int]:
    import datetime as _dt

    iso = mkt.get("endDateIso") or mkt.get("endDate")
    if iso:
        try:
            if iso.endswith("Z"):
                iso = iso[:-1] + "+00:00"
            return int(_dt.datetime.fromisoformat(iso).timestamp())
        except Exception:
            pass

    tail = slug.rsplit("-", 1)[-1]
    if tail.isdigit():
        return int(tail)
    return None


def get_top_of_book(
    token_id: str,
    session: Optional[requests.Session] = None,
) -> Optional[TopOfBook]:
    """Return best bid/ask for a CLOB token. Prices are in dollars (0..1)."""
    sess = session or requests.Session()
    try:
        r = sess.get(f"{CLOB_BASE}/book", params={"token_id": token_id}, timeout=6)
    except requests.RequestException as e:
        log.debug("clob book %s network error: %s", token_id, e)
        return None
    if r.status_code != 200:
        return None
    try:
        data = r.json()
    except ValueError:
        return None

    bids = data.get("bids") or []
    asks = data.get("asks") or []
    bid = _top(bids, pick_max=True)
    ask = _top(asks, pick_max=False)
    return TopOfBook(
        bid=bid[0] if bid else None,
        ask=ask[0] if ask else None,
        bid_size=bid[1] if bid else None,
        ask_size=ask[1] if ask else None,
    )


def _top(levels: list, pick_max: bool) -> Optional[tuple[float, float]]:
    """Return (price, size) of the best level. CLOB returns unordered lists."""
    if not levels:
        return None
    best: Optional[tuple[float, float]] = None
    for lvl in levels:
        try:
            p = float(lvl.get("price"))
            s = float(lvl.get("size"))
        except (TypeError, ValueError):
            continue
        if best is None or (pick_max and p > best[0]) or (not pick_max and p < best[0]):
            best = (p, s)
    return best


def get_resolution(
    slug: str,
    session: Optional[requests.Session] = None,
) -> Optional[str]:
    """Return 'UP', 'DOWN', 'VOID', or None if the market hasn't resolved."""
    result, _ = get_resolution_detailed(slug, session=session)
    return result


def get_resolution_via_clob(
    condition_id: str,
    token_id: str,
    session: Optional[requests.Session] = None,
) -> Optional[dict]:
    """Definitive Polymarket resolution via the CLOB /markets/<cid>.

    Matches the pattern copy_trader.py / momentum_engine.py use -- they
    observed 100% resolution accuracy against on-chain. Gamma's
    outcomePrices is orderbook-derived and can lag or stale; the CLOB
    endpoint exposes a per-token ``winner: bool`` flag that reflects
    the actual settlement record.

    Returns ``{resolved: bool, our_token_won: bool, winning_outcome,
    winning_token_id}`` on success, or None if not yet resolved.
    """
    if not condition_id:
        return None
    sess = session or requests.Session()
    try:
        r = sess.get(f"{CLOB_BASE}/markets/{condition_id}", timeout=10)
    except requests.RequestException as e:
        log.debug("clob markets %s network error: %s", condition_id, e)
        return None
    if r.status_code != 200:
        return None
    try:
        market = r.json()
    except ValueError:
        return None

    # Must be closed to be resolved.
    if not market.get("closed"):
        return None

    tokens = market.get("tokens") or []
    winning_token = None
    for t in tokens:
        if t.get("winner") is True:
            winning_token = t
            break
    if winning_token is None:
        # Closed but no winner flag set yet (very brief settlement window).
        return None

    winning_token_id = str(winning_token.get("token_id", "")).strip()
    our_tid = str(token_id).strip() if token_id else ""
    our_token_won = (our_tid == winning_token_id) if our_tid else None

    return {
        "resolved": True,
        "our_token_won": our_token_won,
        "winning_outcome": winning_token.get("outcome"),
        "winning_token_id": winning_token_id,
    }


def get_market_settled_prices(
    slug: str,
    session: Optional[requests.Session] = None,
) -> Optional[dict[str, float]]:
    """Return ``{token_id: price}`` for a closed market, or None.

    Prices sum to ~1.0 only when the oracle has fully settled the
    market (winner=1.0, loser=0.0). Between market close and oracle
    finalisation, and/or after redemption, Gamma can report transient
    states like [0, 0.01] that should be *ignored* -- the caller is
    expected to require a near-unity sum before acting.
    """
    sess = session or requests.Session()
    try:
        r = sess.get(f"{GAMMA_BASE}/events", params={"slug": slug}, timeout=6)
    except requests.RequestException as e:
        log.debug("gamma settled-prices %s network error: %s", slug, e)
        return None
    if r.status_code != 200:
        return None
    try:
        data = r.json()
    except ValueError:
        return None

    events = data if isinstance(data, list) else [data] if isinstance(data, dict) else []
    for ev in events:
        for mkt in ev.get("markets", []) or []:
            if not bool(mkt.get("closed")):
                continue
            token_ids = _decode_list(mkt.get("clobTokenIds") or mkt.get("tokens"))
            prices = _decode_list(mkt.get("outcomePrices"))
            if not token_ids or not prices or len(token_ids) != len(prices):
                continue
            out: dict[str, float] = {}
            for tid, pr in zip(token_ids, prices):
                try:
                    out[str(tid)] = float(pr)
                except (TypeError, ValueError):
                    continue
            if out:
                return out
    return None


def get_token_price_final(
    slug: str,
    token_id: str,
    session: Optional[requests.Session] = None,
) -> Optional[float]:
    """Legacy single-token lookup. Prefer ``get_market_settled_prices``
    so the caller can check that the price pair sums to ~1 (oracle-
    settled) and avoid transient book-residual states like [0, 0.01].
    """
    prices = get_market_settled_prices(slug, session=session)
    if not prices:
        return None
    return prices.get(str(token_id))


def get_resolution_detailed(
    slug: str,
    session: Optional[requests.Session] = None,
) -> tuple[Optional[str], dict]:
    """Like ``get_resolution`` but also return the raw Gamma fields
    we used to make the decision. Logged by the live engine so that
    any future mismatch with on-chain can be diagnosed directly from
    Railway output without a re-query.
    """
    sess = session or requests.Session()
    try:
        r = sess.get(f"{GAMMA_BASE}/events", params={"slug": slug}, timeout=6)
    except requests.RequestException as e:
        log.debug("gamma resolve %s network error: %s", slug, e)
        return None, {}
    if r.status_code != 200:
        return None, {}
    try:
        data = r.json()
    except ValueError:
        return None, {}

    events = data if isinstance(data, list) else [data] if isinstance(data, dict) else []
    for ev in events:
        for mkt in ev.get("markets", []) or []:
            res = _parse_resolution(mkt)
            if res is not None:
                raw = {
                    "closed": mkt.get("closed"),
                    "outcomes": mkt.get("outcomes"),
                    "outcomePrices": mkt.get("outcomePrices"),
                    "winnerOutcome": mkt.get("winnerOutcome"),
                    "resolvedOutcome": mkt.get("resolvedOutcome"),
                    "umaResolutionStatus": mkt.get("umaResolutionStatus"),
                }
                return res, raw
    return None, {}


def _parse_resolution(mkt: dict) -> Optional[str]:
    """Read outcomePrices to determine if/how the market resolved.

    Polymarket books often converge to [~0.99, ~0.01] in the final
    seconds of a 5m market when one side is obviously winning on BTC
    spot -- but that's not settlement. If BTC whipsaws, the on-chain
    resolution can flip relative to what the book implied. To avoid
    booking pre-settlement book reads as final, require the market's
    ``closed`` flag to be true before trusting extreme prices.
    """
    if not bool(mkt.get("closed")):
        return None

    prices = _decode_list(mkt.get("outcomePrices"))
    if not prices or len(prices) < 2:
        return None
    outcomes = _decode_list(mkt.get("outcomes"))
    up_idx = _up_outcome_index(outcomes)
    down_idx = 1 - up_idx
    try:
        up_price = float(prices[up_idx])
        down_price = float(prices[down_idx])
    except (TypeError, ValueError):
        return None

    if up_price >= 0.99 and down_price <= 0.01:
        return "UP"
    if down_price >= 0.99 and up_price <= 0.01:
        return "DOWN"

    # Closed with a ~50/50 split -> voided.
    if abs(up_price - 0.5) < 0.05 and abs(down_price - 0.5) < 0.05:
        return "VOID"
    return None
