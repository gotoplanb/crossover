# Marvel API fixtures

**Hand-authored to the documented response shape, not recorded from a live
call.** No Marvel developer key was available when this project was built
(docs/gates.md), so these prove that parsing and the Gate B traceability
*mechanism* work. They are not evidence about any real issue's digital id.

Replace them with real recordings as soon as a key exists — the shapes should
match, and the tests should keep passing unchanged.

Deliberate properties worth preserving in any replacement:

- `king_in_black_1` has a real `digitalId` → the linkable path.
- `king_in_black_2` has `digitalId: 0`, which is how Marvel says "no digital
  edition". Parsing it as the integer 0 and building a reader URL from it would
  be a Gate B bug, so a fixture must keep exercising it.
- `king_in_black_2` also uses Marvel's `image_not_available` placeholder path,
  so the missing-cover treatment stays covered.
- The marvel.com issue id appears only inside `urls`, never as its own field.
