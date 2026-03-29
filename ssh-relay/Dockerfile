FROM alpine:3.19

RUN apk add --no-cache socat

RUN adduser -D -u 1000 relay
USER relay

EXPOSE 2222

# Forward all connections on :2222 to github.com:22
CMD ["socat", "TCP-LISTEN:2222,fork,reuseaddr", "TCP:github.com:22"]
