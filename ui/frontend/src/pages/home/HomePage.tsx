import "./home.css";
import { Nav } from "./Nav";
import { useWarmSurgBot } from "../../surgbot/warm";
import { Hero } from "./Hero";
import { ProblemSection } from "./ProblemSection";
import { SolutionSection } from "./SolutionSection";
import { HowItWorksSection } from "./HowItWorksSection";
import { ArchitectureSection } from "./ArchitectureSection";
import { LivingGraphSection } from "./LivingGraphSection";
import { AgentsSection } from "./AgentsSection";
import { TechSection } from "./TechSection";
import { DemoCtaSection } from "./DemoCtaSection";
import { Footer } from "./Footer";

export function HomePage() {
  // Starts SurgBot's container while the visitor reads, so the voice panel is
  // not a cold start when they get to it.
  useWarmSurgBot();

  return (
    <div className="home">
      <Nav />
      <Hero />
      <ProblemSection />
      <SolutionSection />
      <HowItWorksSection />
      <ArchitectureSection />
      <LivingGraphSection />
      <AgentsSection />
      <TechSection />
      <DemoCtaSection />
      <Footer />
    </div>
  );
}
