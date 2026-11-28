// The kind of the environment in use (dev · staging · prod), for every "start a run" button: in
// prod they say so ("Run in prod") and take the --stop tone (`.btn-run.prod`).
import { createContext, useContext } from "react";

export const EnvKindContext = createContext<string | null>(null);

/** True when the current workspace environment is a production one. */
export function useProd(): boolean {
  return useContext(EnvKindContext) === "prod";
}
