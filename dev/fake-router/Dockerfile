# Test double for an Asus router: BusyBox + Dropbear + fake `nvram` and `wl`.
# Used only by dev/docker-compose.dev.yml to test the stack without real hardware.
FROM alpine:3.24
# Like a real Asus, the SSH login is uid 0 and nvram/wl are on the default PATH.
RUN apk add --no-cache dropbear \
 && sed -i 's/^root:[^:]*:/root:*:/' /etc/shadow \
 && mkdir -p /etc/dropbear /var/lib/misc
COPY nvram wl /usr/bin/
COPY entrypoint.sh /entrypoint.sh
RUN chmod +x /usr/bin/nvram /usr/bin/wl /entrypoint.sh
EXPOSE 22
ENTRYPOINT ["/entrypoint.sh"]
