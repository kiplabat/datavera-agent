import React from "react";
import { createRoot } from "react-dom/client";
import "@fontsource/dm-sans/400.css";
import "@fontsource/dm-sans/500.css";
import "@fontsource/dm-sans/700.css";
import "@fontsource/fraunces/600.css";
import "./styles.css";
import App from "./App";
import { AppProvider } from "./store";

createRoot(document.getElementById("root")!).render(
  <AppProvider>
    <App />
  </AppProvider>
);
