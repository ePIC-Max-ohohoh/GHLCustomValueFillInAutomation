# Asset sourcing SOP (Phase 1 reference only)

Phase 1 does **not** discover, transform, upload, or validate assets. Operators must place the finished value in `data/advisors.csv`.

Source rules retained from the August 2026 SOP:

- Check the advisor's ClickUp Account Record first.
- WebPrez links must come from that advisor's sub-account and use **Video Page Hypertext Link WITH Viewing Notice**.
- Vimeo values use `https://player.vimeo.com/video/VIDEO_ID?title=0&byline=0&portrait=0&autoplay=1` inside the standard iframe wrapper.
- Images and real PDFs are hosted in the advisor's own GHL Media Storage.
- `bridgepoint_brochure` may be a public Gamma page rather than a PDF.
- `email_unsubscribe_link` is advisor-specific and ends with `?email={{contact.email}}`.
- `email_signature` is supplied as finished mobile-safe table HTML; Phase 1 does not generate it.

If an asset is missing or uncertain, flag it to the responsible operator. Never substitute another advisor's link.

