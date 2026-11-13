# Public review widget captures

The three `public-reviews-<sku>.json` fixtures retain review fields from public Ozon review pages inspected on 2026-11-13.

The source was each page's `div.client-state [id^="state-"][data-state]`, captured through the signed-in browser. No composer or entrypoint response supplied these fixtures.

The original artifacts are in `/workspace/codex-ozon-reviews/inspect/fixtures/`, with the same SKU and a `.widgetStates.json` suffix. `MARKUP.md` in the parent directory records the inspection. These repository copies omit user state, tracking, actions, and unused display fields. Review text, authors' public display names, UUIDs, ratings, dates, votes, photos, variant attributes, aggregates, and pagination remain unchanged.

| SKU | Rating | Total reviews | Captured reviews | Next page |
| --- | --- | --- | --- | --- |
| 185618681 | 4.9 | 398 | 30 | Declared |
| 3841862824 | 4.5 | 14 | 14 | None |
| 507239415 | 5.0 | 810 | 30 | Declared |

`tests/review_fixtures.py` renders these states as escaped `data-state` attributes inside a hidden client-state container. Browser tests intercept every request. Pagination tests split the first capture into two pages with one overlapping review and retain its declared next-page query. They verify navigation and deduplication, but do not claim a live page-two capture.

`public-reviews-no-reviews.html` retains the exact error widget and `state-error-3036935-default-1` declaration from `/workspace/codex-ozon-reviews/inspect/5745227761-failed.html`, captured on 2026-11-13. It keeps the document title `OZON` and the hidden `div.client-state` container. Scripts, styles, navigation, account state, and unrelated widgets are omitted. The error declares `Такой страницы не существует` with subtitle `потому что у товара нет отзывов`. Browser tests serve this fixture as HTTP 200 with every request intercepted.
