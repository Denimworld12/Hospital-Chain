import heart from "@assets/heart-anatomical.svg";
import pillLeaf from "@assets/pill-leaf.svg";
import { Blob, Eyebrow, Illustration } from "./ui.jsx";

const WORDS = ["Zero", "One", "Two", "Three", "Four", "Five", "Six", "Seven", "Eight", "Nine", "Ten"];

export default function Hero({ engine, hospitals }) {
  const count = hospitals == null ? "A chain of" : `${WORDS[hospitals] || hospitals}`;
  return (
    <section className="hero">
      <div className="hero__copy">
        <Eyebrow>MedChain / decentralized learning for hospitals</Eyebrow>
        <h1 className="hero__headline">
          Collaborative
          <br />
          Training
          <br />
          Network
        </h1>
        <p className="script hero__tagline">learning together, data stays home</p>
        <p className="hero__body">
          {count} hospital{hospitals === 1 ? "" : "s"} train one heart-disease model together, and hospitals
          can join or leave while they do. Patient records never leave the building: only model updates
          travel between neighbouring nodes.
        </p>
        <p className="tag">Engine: {engine}</p>
      </div>
      <div className="hero__divider" aria-hidden="true">
        <Blob color="sage" className="hero__blob hero__blob--sage" />
        <Blob color="gold" className="hero__blob hero__blob--gold" />
      </div>
      <div className="hero__art">
        <Illustration src={heart} rotate={6} className="hero__heart" />
      </div>
      <Illustration src={pillLeaf} rotate={-8} className="hero__accent" />
    </section>
  );
}
