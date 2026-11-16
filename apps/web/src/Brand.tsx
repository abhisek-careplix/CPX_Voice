import logoDark from "./assets/careplix-logo-dark.png";
import logo from "./assets/careplix-logo.png";

/** The CarePlix logo with the product name. The logo has a light-surface and a dark-surface file. */
export function BrandLockup({ size = "md" }: { size?: "md" | "lg" }) {
  const height = size === "lg" ? 34 : 22;
  return (
    <span className="brand-lockup">
      <img className="brand-logo brand-logo-light" src={logo} alt="CarePlix" style={{ height }} />
      <img className="brand-logo brand-logo-dark" src={logoDark} alt="" aria-hidden style={{ height }} />
      <span className="brand-product" style={size === "lg" ? { fontSize: 20, lineHeight: "28px" } : undefined}>
        Voice
      </span>
    </span>
  );
}
