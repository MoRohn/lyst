# 0004. License: AGPL-3.0-only

**Status.** Superseded Apache-2.0 on 2026-11-04 by Rohn's decision; AGPL-3.0 is the go-forward license.

**Decision.** Lyst is licensed under the GNU Affero General Public License, version 3 only
(`AGPL-3.0-only`). Source files carry an SPDX header. Dependencies must be open source and
AGPLv3-compatible; `scripts/licenses.py` enforces it and writes `docs/SBOM.md`.

**What it means.** Anyone may use, modify and self-host Lyst. Anyone who runs a modified version as a
network service must offer that version's source to its users (section 13). Commercial licensing on other
terms remains possible for the copyright holder.

**Notes.** "Only" rather than "or later" follows the decision as given ("AGPL-3.0"); switching to
`AGPL-3.0-or-later` is a one-line change if preferred. Code vendored from ai-analytics and designs ported from
Acumane are the same owner's work, so they can be relicensed. Contributions from others will need a
contributor agreement if the owner wants to keep the option of other licenses.
