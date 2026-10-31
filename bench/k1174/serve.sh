#!/bin/sh
exec python3 -m http.server 8901 --directory "$(dirname "$0")"
