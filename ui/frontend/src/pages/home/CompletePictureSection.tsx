import { CompletePictureFlow } from "./CompletePictureFlow";

export function CompletePictureSection() {
  return (
    <section className="home__section" id="complete-picture">
      <span className="home__eyebrow">SurgOS</span>
      <h2 className="home__headline" style={{ fontSize: 34, maxWidth: 620 }}>
        The Complete Picture
      </h2>
      <p className="home__lede" style={{ marginBottom: 16, maxWidth: 700 }}>
        Two independent workflows, one shared system underneath.
      </p>
      <CompletePictureFlow />
    </section>
  );
}
