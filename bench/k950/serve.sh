#!/bin/sh
exec python3 -m http.server 8902 --directory "$(dirname "$0")"
