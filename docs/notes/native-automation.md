# Helix Native automation, MIDI CC and state (own summary)

Status: [T009](../../board/TASKS.md#t009 "Pull the Native Pilot's Guide automation details and fretwire's…"), research. Sources are Line 6 pages summarised in our own words. Caveat: the Pilot's Guide 3.80 PDF could not be text-extracted in this workspace (no PDF renderer), so page-level detail from the guide itself is NOT yet verified. The facts below come from the KB and forum pages that quote or paraphrase it. Someone with a PDF reader should confirm against the guide's "Automation/Controller Assign tab" section (forum cites page 30).

## Automation slots

- Each plugin instance exposes 16 knob slots and 16 switch slots. A knob or switch in any block can be assigned to one slot; the DAW then sees the slot as an automatable parameter. Source: [Helix Native FAQ](https://kb.line6.com/helix-native-faq), [forum: automation](https://line6.com/support/topic/31714-unleash-the-power-of-helix-native-automation-in-your-tracks), [forum: MIDI in Logic](https://line6.com/support/topic/47525-helix-native-midi-operations-in-logic-pro-x/).
- Assignment is done in the plugin's Automation/Controller Assign tab (or by right-clicking a control). Slot names in the DAW are generic numbers, so the host needs its own mapping.
- Forum report: when a block's model is swapped, Native tries to keep the slot assignment. Treat as unverified; test it in the native-engine spike ([T002](../../board/TASKS.md#t002 "Spike: can pedalboard write a whole preset into Helix Native's saved…")).
- Consequence for us: 16 knobs + 16 switches caps what one fixed chain can expose. A fixed template must pick at most 32 controls; model changes need either state loading or a different template.

## MIDI CC

- Native accepts CC for block bypass (assign a CC number in the Controller Assign tab) and for parameters (choose MIDI CC as controller, set CC#, min, max). Snapshot recall is CC 69 (value 0 = snapshot 1, 1 = snapshot 2, ...). Many CCs are ignored because hardware reserves them. Source: [KB: MIDI operations in a DAW](https://kb.line6.com/helix-native-midi-operations-in-a-daw).
- MIDI reaches the plugin only if the host routes a MIDI track or instrument-style input to it. Host-dependent; relevant to pedalboard, which must be shown to deliver MIDI to the plugin.
- MIDI CC for bypass and parameters exists from Native 1.60 onward per the [forum thread](https://line6.com/support/topic/47525-helix-native-midi-operations-in-logic-pro-x/).
- The FAQ's "no MIDI at launch" line is historical and superseded by the KB page.

## State saving

- Not documented in the sources reached: what the plugin state blob contains, and whether assignments are stored inside it. The KB MIDI page says nothing about persistence. This is exactly [T002](../../board/TASKS.md#t002 "Spike: can pedalboard write a whole preset into Helix Native's saved…")'s question; answer it empirically (save state from pedalboard, diff after changing a preset) rather than from docs.
- Presets can be imported from hardware `.hlx` with compatibility limits: send/return, global EQ, Command Center and some hardware-only settings are unavailable or bypassed ([FAQ](https://kb.line6.com/helix-native-faq)). Impact: golden templates should avoid those features.

## Licensing facts relevant to automation

- Account-based authorisation, up to four computers per licence ([FAQ](https://kb.line6.com/helix-native-faq)). Many parallel render workers on separate machines may hit this limit.
- Licence tied to eligible hardware purchase in some cases; returning hardware can revoke it (same FAQ).

## EULA flags (not legal advice)

See `/Users/jason/Documents/code/helix_pedal/docs/notes/line6-eula-flags.md`.
