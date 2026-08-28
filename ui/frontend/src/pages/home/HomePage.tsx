import "./home.css";
import { Nav } from "./Nav";
import { Hero } from "./Hero";
import { ProblemSection } from "./ProblemSection";
import { SolutionSection } from "./SolutionSection";
import { HowItWorksSection } from "./HowItWorksSection";
import { SurgBotSection } from "./SurgBotSection";
import { CompletePictureSection } from "./CompletePictureSection";
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
      <SolutionSection />
      <HowItWorksSection />
      <AgentsSection />
      <LivingGraphSection />
      <SurgBotSection />
      <CompletePictureSection />
      <TechSection />
      <DemoCtaSection />
      <Footer />
    </div>
  );
}
