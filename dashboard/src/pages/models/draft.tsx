import {
  createContext,
  useContext,
  useState,
  type Dispatch,
  type ReactNode,
  type SetStateAction,
} from "react";
import type { MaintenanceResult } from "../../api/administration";
interface ModelDraft {
  profile: string;
  toml: string;
  notice: string | null;
  setupResult: MaintenanceResult | null;
}
type DraftState = {
  draft: ModelDraft;
  setDraft: Dispatch<SetStateAction<ModelDraft>>;
};
const Context = createContext<DraftState | null>(null);
const initial: ModelDraft = {
  profile: "",
  toml: "",
  notice: null,
  setupResult: null,
};
export function ModelDraftProvider({ children }: { children: ReactNode }) {
  const [draft, setDraft] = useState(initial);
  return (
    <Context.Provider value={{ draft, setDraft }}>{children}</Context.Provider>
  );
}
export function useModelDraft() {
  const context = useContext(Context);
  const [draft, setDraft] = useState(initial);
  return context ?? { draft, setDraft };
}
