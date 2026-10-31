# Bench vendored artifacts

The `kaplay.js` / `kaplay.fix.js` bundles and the `9slice.png`,
`Anton-Regular.ttf`, and `Nabla.ttf` test assets under `bench/k*/` are
build / test artifacts of [kaplayjs/kaplay](https://github.com/kaplayjs/kaplay),
which is MIT licensed:

```
MIT License

Copyright (c) 2025 KAPLAY Team

Permission is hereby granted, free of charge, to any person obtaining a copy
of this software and associated documentation files (the "Software"), to deal
in the Software without restriction, including without limitation the rights
to use, copy, modify, merge, publish, distribute, sublicense, and/or sell
copies of the Software, and to permit persons to whom the Software is
furnished to do so, subject to the following conditions:

The above copyright notice and this permission notice shall be included in all
copies or substantial portions of the Software.

THE SOFTWARE IS PROVIDED "AS IS", WITHOUT WARRANTY OF ANY KIND, EXPRESS OR
IMPLIED, INCLUDING BUT NOT LIMITED TO THE WARRANTIES OF MERCHANTABILITY,
FITNESS FOR A PARTICULAR PURPOSE AND NONINFRINGEMENT. IN NO EVENT SHALL THE
AUTHORS OR COPYRIGHT HOLDERS BE LIABLE FOR ANY CLAIM, DAMAGES OR OTHER
LIABILITY, WHETHER IN AN ACTION OF CONTRACT, TORT OR OTHERWISE, ARISING FROM,
OUT OF OR IN CONNECTION WITH THE SOFTWARE OR THE USE OR OTHER DEALINGS IN THE
SOFTWARE.
```

Pinned sources (`bench/<id>/repro.json` records `buggy_sha` / `fix_sha`):

| case | bundle built at |
| ---- | --------------- |
| k1174 | buggy `a9680214d3f8ee8a28d5f2217127daca33623ccf` / fix `ec5d7c4648431c8955b00af00cb911a3eb9d08fb` |
| k950 | buggy `2b07d92070e5537e2b0670f4a473952c4e86c51b` / fix `231b23f07e5af855ad46d1466228a553b6b26e3d` (plus `examples/fonts/*.ttf` from the fix commit) |
| k864 | buggy `1e1d6ab1dca78bced6da97b27e65bf3826da509b` / fix `229a9bd74bf6c8f8f1c2fdf4b3644e67e9e2169a` (plus `examples/sprites/9slice.png`) |
| k658 | buggy `4c9ee05784bf0836d6b5f50d8fc6eb51c723f759` / fix `c7e91c6416f2a745d4dd228faf3ed28f230ca531` (plus `examples/sprites/9slice.png`) |

The kaplay repository itself is never vendored here (it lives in `../kaplay`).
