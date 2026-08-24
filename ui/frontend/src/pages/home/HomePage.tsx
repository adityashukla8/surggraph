import "./home.css";
import { Nav } from "./Nav";
import { Hero } from "./Hero";
import { ProblemSection } from "./ProblemSection";
import { HowItWorksSection } from "./HowItWorksSection";
import { LivingGraphSection } from "./LivingGraphSection";
import { AgentsSection } from "./AgentsSection";
import { TechSection } from "./TechSection";
import { DemoCtaSection } from "./DemoCtaSection";
import { Footer } from "./Footer";

export function HomePage() {
  return (
    <div className="home">
      <Nav />
      <Hero />
      <ProblemSection />
      <HowItWorksSection />
      <LivingGraphSection />
      <AgentsSection />
      <TechSection />
      <DemoCtaSection />
      <Footer />
    </div>
  );
}
