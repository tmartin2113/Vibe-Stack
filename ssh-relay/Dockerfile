FROM alpine:3.19

RUN apk add --no-cache socat

EXPOSE 22

# Forward all connections on :22 to github.com:22
CMD ["socat", "TCP-LISTEN:22,fork,reuseaddr", "TCP:github.com:22"]
