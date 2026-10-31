#!/bin/sh
exec python3 -m http.server 8904 --directory "$(dirname "$0")"
