# Line 6 EULA flags for automation and protocol use (not legal advice)

Status: T009. The only Line 6 licence text reached was the generic software EULA at [line6.com/software/eulas/standard.html](https://line6.com/software/eulas/standard.html) (the page names YGG, Yamaha Guitar Group). It may not be the exact EULA shown in the Helix Native installer or HX Edit; the installer text must be read by the human before any decision. Line 6's [API Developer Agreement](https://line6.com/api/developeragreement.html) has similar wording but applies to their web API, not to us.

## Clauses that matter (paraphrased)

1. No reverse engineering, decompiling or disassembling, nor discovering underlying algorithms, nor derivative works of the software, except as law expressly permits. Matters for: firmware decompilation (already ruled out), and for how fretwire-style protocol recovery is described. fretwire/TonePush say they used USB traffic observation and no vendor source; we should only use their published results, not extend by disassembling HX Edit or firmware.
2. Personal, limited, non-sublicensable licence, one copy, multiple machines but one at a time. The Native FAQ says four authorised computers. Parallel render farms may breach either.
3. Prohibition on ASP, service bureau, cloud or hosted use without written authorisation. Flag: running Native on a hosted service that renders for other people would likely fall under this. Local use by a licensed user driving it from Python looks like ordinary use, but nothing in the text we reached speaks about scripted or automated hosting. Ask Line 6 if a hosted or shared-worker mode is ever wanted.
4. No network sharing of the software across multiple devices.
5. Nothing found on ownership of audio rendered by the plugin, or on using renders as ML training data. The plan's training-on-Native-renders risk stays open; the human should ask Line 6 or train from hardware renders.
6. Reading the model catalog from HX Edit's resource folder at runtime: not addressed in text reached; HX Edit's own EULA must be read. Keep copies out of the repo regardless.

## Recommendations

- Human to read the EULAs displayed by the Native and HX Edit installers and record the verdicts in docs/DECISIONS.md.
- Keep Native automation (16+16 slots, MIDI CC, pedalboard hosting) as documented user-facing features; they are not restricted by anything found.
- Do not publish a hosted render service. Do not ship trained models derived from Native renders until item 5 is answered.
- Do not decompile HX Edit or Native to learn the state-blob format; inspect the blobs the plugin itself produces with its host API only (this is T002's approach) and treat the result as still subject to clause 1 until the installer EULA is read.

## Human review (2026-10-15)

The human read the EULAs shipped with the Helix Native and HX Edit installers and found no problem for this project, including local scripted use and training on renders made with their own licence. Recorded in docs/DECISIONS.md. This closes T017.
