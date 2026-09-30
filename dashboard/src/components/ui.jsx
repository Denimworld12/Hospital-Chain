import waveRaw from "@assets/wave-divider.svg?raw";
import blobGoldRaw from "@assets/blob-gold.svg?raw";
import blobSageRaw from "@assets/blob-sage.svg?raw";
import blobOrangeRaw from "@assets/blob-orange.svg?raw";

export function PillButton({ variant = "primary", children, ...props }) {
  return (
    <button type="button" className={`pill pill--${variant}`} {...props}>
      {children}
    </button>
  );
}

export function Eyebrow({ children, as: Tag = "p", className = "" }) {
  return <Tag className={`eyebrow ${className}`}>{children}</Tag>;
}

export function StatusDot({ status, label = true }) {
  const alive = status === "alive";
  return (
    <span className={`status status--${alive ? "alive" : "down"}`} data-status={alive ? "alive" : "down"}>
      <span className="status__dot" aria-hidden="true" />
      {label && <span className="status__label">{alive ? "Alive" : "Down"}</span>}
    </span>
  );
}

export function Illustration({ src, rotate = 0, className = "", style = {} }) {
  return (
    <img
      src={src}
      alt=""
      aria-hidden="true"
      className={`illo ${className}`}
      style={{ ...style, transform: `rotate(${rotate}deg) ${style.transform || ""}` }}
    />
  );
}

// Organic wavy divider, solid-filled with the colour of the section below it.
const wavePath = waveRaw.match(/ d="([^"]+)"/)[1];
export function WaveDivider({ fill = "var(--color-pure-white)", flip = false }) {
  return (
    <svg
      className={`wave-divider${flip ? " wave-divider--flip" : ""}`}
      viewBox="0 0 1440 200"
      preserveAspectRatio="none"
      aria-hidden="true"
    >
      <path d={wavePath} style={{ fill }} />
    </svg>
  );
}

export function SectionHeading({ title, tagline, eyebrow }) {
  return (
    <header className="section-heading">
      {eyebrow && <Eyebrow>{eyebrow}</Eyebrow>}
      <h2 className="heading">{title}</h2>
      {tagline && <p className="script">{tagline}</p>}
    </header>
  );
}

export function formatBytes(n) {
  if (n == null) return "-";
  return n.toLocaleString("en-US");
}

export function shortId(id) {
  return (id || "").replace(/^hospital-/i, "").toUpperCase();
}

// Organic accent blob inlined so it can be stretched (e.g. into the hero's wavy vertical divider).
const BLOBS = Object.fromEntries(
  Object.entries({ gold: blobGoldRaw, sage: blobSageRaw, orange: blobOrangeRaw }).map(([k, raw]) => [
    k,
    { d: raw.match(/ d="([^"]+)"/)[1], fill: raw.match(/ fill="([^"]+)"/)[1] },
  ]),
);
export function Blob({ color = "gold", className = "", style }) {
  const b = BLOBS[color];
  return (
    <svg className={`blob ${className}`} style={style} viewBox="0 0 400 400" preserveAspectRatio="none" aria-hidden="true">
      <path d={b.d} fill={b.fill} />
    </svg>
  );
}
