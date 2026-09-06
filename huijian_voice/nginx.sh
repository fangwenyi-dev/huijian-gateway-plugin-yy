#!/usr/bin/with-contenv bash
exec nginx -c /etc/huijian/nginx.conf -g 'daemon off;'
