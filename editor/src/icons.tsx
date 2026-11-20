// Built-in agent icons (24×24, stroke = currentColor, so they follow the theme). An agent's `icon` is
// one of these ids or a short text such as an emoji; without one, agents show the robot.

const P: Record<string, string[]> = {
  robot: ["M12 3v3", "M7 7h10a3 3 0 0 1 3 3v6a3 3 0 0 1-3 3H7a3 3 0 0 1-3-3v-6a3 3 0 0 1 3-3z",
    "M9.5 12.5h.01", "M14.5 12.5h.01", "M9.5 16h5", "M2 12v3", "M22 12v3"],
  search: ["M11 4a7 7 0 1 0 0 14 7 7 0 0 0 0-14z", "M20 20l-4-4"],
  code: ["M8 7l-5 5 5 5", "M16 7l5 5-5 5", "M14 4l-4 16"],
  test: ["M9 3h6", "M10 3v6l-5 9a2 2 0 0 0 1.8 3h10.4a2 2 0 0 0 1.8-3l-5-9V3", "M7.5 15h9"],
  shield: ["M12 3l8 3v6c0 5-3.5 8-8 9-4.5-1-8-4-8-9V6z", "M9 12l2 2 4-4"],
  doc: ["M7 3h7l5 5v13H7z", "M14 3v5h5", "M10 13h6", "M10 17h6"],
  chat: ["M4 5h16v11H9l-5 4z", "M8 9h8", "M8 12h5"],
  database: ["M12 3c4.4 0 8 1.3 8 3s-3.6 3-8 3-8-1.3-8-3 3.6-3 8-3z", "M4 6v12c0 1.7 3.6 3 8 3s8-1.3 8-3V6",
    "M4 12c0 1.7 3.6 3 8 3s8-1.3 8-3"],
  cloud: ["M7 18h10a4 4 0 0 0 .5-8 6 6 0 0 0-11.5 1.5A3.3 3.3 0 0 0 7 18z"],
  git: ["M6 3v12", "M6 15a3 3 0 1 0 0 6 3 3 0 0 0 0-6z", "M18 6a3 3 0 1 0 0 6 3 3 0 0 0 0-6z",
    "M18 12c0 3-3 4-6 4H9"],
  bug: ["M8 8h8v7a4 4 0 0 1-8 0z", "M9 8a3 3 0 0 1 6 0", "M4 11h4", "M16 11h4", "M4 17h4", "M16 17h4",
    "M12 12v7"],
  chart: ["M4 20h16", "M7 16v-5", "M12 16V6", "M17 16v-8"],
  mail: ["M3 6h18v12H3z", "M3 7l9 6 9-6"],
  globe: ["M12 3a9 9 0 1 0 0 18 9 9 0 0 0 0-18z", "M3 12h18", "M12 3c3 3 3 15 0 18", "M12 3c-3 3-3 15 0 18"],
  brain: ["M9 4a3 3 0 0 0-3 3 3 3 0 0 0-2 5 3 3 0 0 0 2 5 3 3 0 0 0 3 3V4z",
    "M15 4a3 3 0 0 1 3 3 3 3 0 0 1 2 5 3 3 0 0 1-2 5 3 3 0 0 1-3 3V4z", "M9 4h6"],
  wrench: ["M15 4a5 5 0 0 0-4.6 6.9L4 17.3V20h2.7l6.4-6.4A5 5 0 0 0 20 9l-3 1-2-2 1-3z"],
  check: ["M5 12l4 4 10-10"],
  clock: ["M12 3a9 9 0 1 0 0 18 9 9 0 0 0 0-18z", "M12 7v5l3 2"],
  user: ["M12 4a4 4 0 1 0 0 8 4 4 0 0 0 0-8z", "M4 21c1.5-4 4.5-6 8-6s6.5 2 8 6"],
  gear: ["M12 9a3 3 0 1 0 0 6 3 3 0 0 0 0-6z",
    "M12 2v3", "M12 19v3", "M2 12h3", "M19 12h3", "M5 5l2 2", "M17 17l2 2", "M5 19l2-2", "M17 7l2-2"],
  hand: ["M8 11V5a1.5 1.5 0 0 1 3 0v5", "M11 10V4a1.5 1.5 0 0 1 3 0v6", "M14 10V5.5a1.5 1.5 0 0 1 3 0V14",
    "M8 11l-1.5-1.5a1.5 1.5 0 0 0-2.1 2.1L9 17c1.2 1.6 2.6 3 5 3h.5a4.5 4.5 0 0 0 4.5-4.5V14"],
  stop: ["M8 3h8l5 5v8l-5 5H8l-5-5V8z", "M9 9l6 6", "M15 9l-6 6"],
};

export const ICON_IDS = Object.keys(P).filter((k) => k !== "hand" && k !== "stop");

export function Icon({ id, size = 18, className }: { id: string; size?: number; className?: string }) {
  const paths = P[id] ?? P.robot;
  return (
    <svg className={className} width={size} height={size} viewBox="0 0 24 24" fill="none" stroke="currentColor"
      strokeWidth={1.8} strokeLinecap="round" strokeLinejoin="round" aria-hidden="true">
      {paths.map((d, i) => <path key={i} d={d} />)}
    </svg>
  );
}

/** An agent's icon: a built-in id, a short text (emoji), or the robot. */
export function AgentIcon({ icon, size = 18, className }: { icon?: string | null; size?: number; className?: string }) {
  if (icon && !(icon in P)) {
    return <span className={`icon-text ${className ?? ""}`} style={{ fontSize: size * 0.9, lineHeight: 1 }}>{icon}</span>;
  }
  return <Icon id={icon || "robot"} size={size} className={className} />;
}
