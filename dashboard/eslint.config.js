// Minimal lint for the dashboard: ESLint's recommended rules, plus JSX awareness so components used
// only in JSX count as used. `make lint` runs it on changed files only; there is no repo-wide pass.
import js from "@eslint/js";
import react from "eslint-plugin-react";
import globals from "globals";

export default [
  { ignores: ["dist/**", "node_modules/**"] },
  js.configs.recommended,
  {
    files: ["**/*.{js,jsx}"],
    plugins: { react },
    languageOptions: {
      ecmaVersion: 2022,
      sourceType: "module",
      parserOptions: { ecmaFeatures: { jsx: true } },
      globals: { ...globals.browser, ...globals.node },
    },
    rules: {
      "react/jsx-uses-vars": "error",
      "react/jsx-uses-react": "error",
    },
  },
];
