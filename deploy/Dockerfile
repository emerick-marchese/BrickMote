# BrickMote relay server image.
#   docker build -f deploy/Dockerfile -t brickmote-relay .
FROM python:3.12-slim

RUN useradd --system --create-home --home-dir /data brickmote
WORKDIR /app
COPY requirements-relay.txt .
RUN pip install --no-cache-dir -r requirements-relay.txt
COPY brickmote ./brickmote

USER brickmote
ENV BRICKMOTE_RELAY_DATA=/data
VOLUME ["/data"]
# 6568 = port principal, 443 et 80 = ports de secours (comme AnyDesk)
EXPOSE 6568 443 80
ENTRYPOINT ["python", "-m", "brickmote.server"]
CMD ["--ports", "6568,443,80"]
