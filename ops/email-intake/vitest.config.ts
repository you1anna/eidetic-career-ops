import { cloudflareTest, readD1Migrations } from "@cloudflare/vitest-plugin";
import { defineConfig } from "vitest/config";

export default defineConfig(async () => ({
  test: {
    projects: [
      {
        plugins: [cloudflareTest({
          wrangler: { configPath: "./wrangler.vps.jsonc" },
          // D1 bindings come from the production config; all storage remains local and disposable.
          miniflare: { bindings: {
            BUFFER_TOKEN: "a".repeat(48),
            TEST_MIGRATIONS: await readD1Migrations("./migrations"),
          } },
        })],
        test: { name: "production-buffer", include: ["test/buffered.test.ts"], testTimeout: 20000 },
      },
      {
        plugins: [cloudflareTest({
          wrangler: { configPath: "./wrangler.jsonc" },
          miniflare: { bindings: {
            // Synthetic fixtures for the retained direct-POST pilot, never deployed credentials.
            INTAKE_URL: "https://intake.example.test/v1/email",
            ALLOW_LOOPBACK_HTTP: "false",
            INTAKE_TOKEN: "synthetic-intake-token",
            CF_ACCESS_CLIENT_ID: "synthetic-access-id",
            CF_ACCESS_CLIENT_SECRET: "synthetic-access-secret",
          } },
        })],
        test: { name: "historical-pilot", include: ["test/intake.test.ts"], testTimeout: 20000 },
      },
    ],
  },
}));
