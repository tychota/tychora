import "@testing-library/jest-dom/vitest";
import { cleanup, configure } from "@testing-library/react";
import { afterEach } from "vitest";

// findBy* and waitFor give up after 1 s by default, which loaded CI runners exceed.
configure({ asyncUtilTimeout: 5000 });

afterEach(cleanup);
