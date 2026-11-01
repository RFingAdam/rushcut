# Licensing options (this PR does not change LICENSE)

GitHub currently classifies the repo as license **Other**, even though
[LICENSE](../LICENSE) is MIT text. That usually happens when extra
paragraphs sit under the MIT grant (here: the MAGIX disclaimer and the
voluntary CC0 credit for MarcoRavich/VEGAS-AI-control). SPDX/`licensee`
then refuses to call it MIT.

**This PR does not edit `LICENSE` or `NOTICE`.** The notes below are
options for a later commit.

## Options

### 1. Keep MIT, optional README credit (lowest friction)

- **Legal:** MIT already requires preserving the copyright notice
  (`Copyright (c) 2026 RFingAdam`) in copies.
- **GitHub:** if you also *delete the extra paragraphs from LICENSE*
  (move MAGIX + CC0 notes into NOTICE only), `licensee` will show MIT.
  That is a later commit; not this PR.
- **Adoption:** best. Registries and corporate forks stay easy.

### 2. Apache-2.0 + NOTICE attribution

- Recipients must include NOTICE in distributions.
- Patent grant is extra paperwork; fine if you want NOTICE to travel.

### 3. MIT or Apache-2.0 plus a binding extra clause

- Extra restrictions make GitHub treat it as a custom / Other license.
- Higher fork and registry friction.

### 4. CC-BY-4.0 (do not use for this repo)

- Creative Commons BY is for content, not source code.

### 5. Stay on the current file (MIT + extra paragraphs = GitHub Other)

- Functionally MIT for anyone who reads the text.
- Changing nothing is valid if you do not care about the SPDX badge.

## Practical suggestion

Keep MIT. Optionally move MAGIX / Blackmagic / CC0 notes into NOTICE only
in a dedicated later commit so GitHub shows MIT. Do not change LICENSE in
feature PRs.
