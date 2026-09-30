# MedChain style reference (Pa'lais, medical adaptation)

> Botanical sketchbook dipped in honey - here, a hand-illustrated clinical
> journal dropped into a modern editorial layout.

This is a condensed summary of the Pa'lais style reference, adapted to MedChain.
The colour lanes, type voices, radii and shadow rules are kept exactly; only the
subject matter changes (recipe cards become hospital node cards, and the food
photography role is replaced entirely by SVG line-art, since MedChain uses no
photography). `design/tokens.css` holds the exact values; components read the
tokens, never raw hex.

## Colour lanes

| Token | Hex | MedChain role |
| --- | --- | --- |
| `--color-deep-indigo` | `#234386` | The single brand voice: headlines, nav, links, filled CTA buttons, dark sections, chart lines, illustration strokes. The **only** saturated cool - never add another blue/indigo shade. |
| `--color-sky-blue` | `#6aa8dc` | Secondary line-art stroke and soft illustration hatching only. |
| `--color-honey-gold` | `#ffc400` | Decorative only: blobs (behind the node cluster, hero divider), illustration highlights, chart dots, the "actually sent" bar. **Never a button fill.** |
| `--color-burnt-orange` | `#ed7328` | Outlined secondary-button border + text, linked labels, accent blobs, and the node **down** dot. **Never a filled button background.** |
| `--color-mint-sage` | `#a2d3a6` | Botanical blobs and washes, and the node **alive** dot. |
| `--color-warm-sand` | `#d2b68c` | Sparing accent detail. |
| `--color-ink-black` | `#000000` | Body text, 1px hairline borders, icon strokes. |
| `--color-pure-white` | `#ffffff` | Card surfaces. |
| `--color-cream-paper` | `#fbf9f6` | Page canvas. |
| `--color-soft-shadow` | `#d6d6d6` | Shadow base tone only, in low-opacity card shadows. |

**Layering is cream -> white -> orange -> indigo only.** Never a flat gray
surface. No gradients anywhere. Content is separated by 1px hairline black
borders, not elevation; only cards get a shadow.

## Type voices

| Voice | Face (substitute) | Use |
| --- | --- | --- |
| Display | Bebas Neue (for hwt-artz) | Section headings, all caps, indigo. 32 / 46px, tracking ~0.043em. |
| Hero display | Bebas Neue at hero size | `COLLABORATIVE TRAINING NETWORK` (the captain names Bebas Neue for it), 56-58px range. |
| Display number | Bebas Neue, 88px / 0.82 | The single big number: bytes sent this round. |
| Body / UI | DM Sans (for Axiforma) | Body text 14-16px in ink-black; form inputs. |
| Nav / label | Jost (for ITC Avant Garde) | Nav items and eyebrow labels, always uppercase, the most tracked text: 12px at 0.2em for eyebrows (`HOSPITAL A - 303 PATIENTS`), nav at 0.133em. |
| Script | Caveat | Emotional taglines under a big headline only ("learning together, data stays home"). Never in UI chrome, labels, buttons or body. |
| Journal | IBM Plex Mono | MedChain addition: the audit log's "monospace-adjacent" rows. |

Rules: display fonts only at 32px and up; never body text in a display font;
uppercase labels tracked 0.13-0.27em; nothing tighter than 0.03em or wider than
0.27em. Headlines use the two-voice pattern: condensed display + script accent.

Type scale: caption 12/1.6/1.67px, body-sm 14/1.67/0.99px, body 16/1.5/0.5px,
subheading 24/1.0/6.4px, heading-sm 32/0.9/1.38px, heading 46/1.18/1.98px,
heading-lg 56/1.14/1.74px, display 88/0.82/0px.

## Space, radius, shadow

- 4px base unit: 4, 8, 12, 16, 24, 32, 40, 48, 56, 64, 80, 128.
- Section gap 64px, card padding 16px, element gap 8px, page max-width 1200px.
- Radius: tags 16px, cards 8px, inputs 8px, buttons 32px (pill). Blobs are organic
  SVG paths.
- Shadows on cards only, never buttons or nav: `--shadow-xl-2`
  (`rgba(0,0,0,0.16) -14px 10px 49px`, asymmetric) for hospital cards,
  `--shadow-xl` for quieter cards.

## Components

- **Primary button** (`START TRAINING ROUND`): indigo fill, white text, 32px
  radius, 24px/48px padding, uppercase 13px at ~0.1em, no border, no shadow.
- **Secondary button** (`PAUSE`): transparent, 2px burnt-orange border and
  text, 32px radius, 24px/48px padding, uppercase, optional arrow.
- **Hospital node card**: white, 8px radius, `--shadow-xl-2`, 16px padding,
  sitting on cream, with an eyebrow label above it.
- **Status dot**: mint-sage alive, burnt-orange down.
- **Audit journal table**: 1px hairline black borders, cream rows, IBM Plex Mono.

## Imagery

All imagery is SVG in `assets/svg/`, produced by `assets/generate_svgs.py`; no
photos, no external images. Preview: `assets/preview.html`.

- Line illustrations: 1-1.5px stroke, deep-indigo or sky-blue **only**, **no fills
  ever** (tone comes from engraving-style hatching). Placed at +/-5-10 degrees,
  allowed to bleed off section edges and overlap content, never boxed. Vary scale
  from small accents up to 30-40% of a section.
- Organic blobs: one solid colour each (gold, sage, orange), never gradients,
  never multi-colour. With the section dividers below, these are the only filled
  shapes.
- Section dividers: an organic wavy curve (`wave-divider.svg`, 200px tall), filled
  solid with the colour of the section below it, never a straight hard line.
  `ecg-wave.svg` is a line-art ECG trace used as a lighter in-section rule.

## Composition

- **Hero**: headline left, anatomical heart right, a gold-over-sage blob pair as
  the wavy vertical divider between them.
- **Network topology (home)**: three line-art hospitals joined by a chain, a
  honey-gold blob behind the centre of the cluster, a status dot per node.
- **Training monitor**: one white card per hospital with loss and accuracy line
  charts (indigo lines, honey-gold dots).
- **Communication**: bytes-sent-this-round as the 88px display number, plus a
  bar pair: "full precision would cost" (indigo) vs "actually sent" (gold).
- **Audit log**: the journal table.

## Honest copy

The current engine is a **naive gossip baseline** (neighbour weight averaging).
Wherever the UI or logs name the engine, it says exactly that. Byte counts are
the real payload sizes the nodes put on the wire.
