# Hawkes-GM dashboard — Railway container
FROM node:20-slim

WORKDIR /app

# Install dependencies from the lockfile for reproducible builds.
# Copy the manifests first so the layer cache is reused when only
# application code changes.
COPY package.json package-lock.json ./
RUN npm ci --omit=dev \
    && npm cache clean --force

# Copy the rest of the application.
COPY src ./src
COPY public ./public

# Drop privileges: the node:20-slim image ships with a pre-created
# ``node`` user. Railway containers should not run as root.
RUN chown -R node:node /app
USER node

ENV NODE_ENV=production
# Railway injects PORT at runtime. Keep 3000 as the local default so
# ``docker run -p 3000:3000 ...`` works out of the box.
ENV PORT=3000
EXPOSE 3000

# Lightweight in-container health check so Docker alone (without the
# Railway probe) can mark an unhealthy container for restart.
HEALTHCHECK --interval=30s --timeout=5s --start-period=20s --retries=3 \
    CMD node -e "require('http').get('http://127.0.0.1:'+process.env.PORT+'/health',r=>process.exit(r.statusCode===200?0:1)).on('error',()=>process.exit(1))"

CMD ["node", "src/server.js"]
