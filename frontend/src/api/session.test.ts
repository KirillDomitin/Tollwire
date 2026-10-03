import { authFetch, getAccessToken, refreshAccessToken, setAccessToken } from "./session";

function json(body: unknown, status = 200): Response {
  return new Response(JSON.stringify(body), { status, headers: { "Content-Type": "application/json" } });
}

afterEach(() => {
  vi.unstubAllGlobals();
  setAccessToken(null);
});

it("shares one refresh between concurrent callers (refresh token is single-use)", async () => {
  const fetchMock = vi.fn(async () => json({ access_token: "fresh" }));
  vi.stubGlobal("fetch", fetchMock);
  const tokens = await Promise.all([refreshAccessToken(), refreshAccessToken(), refreshAccessToken()]);
  expect(tokens).toEqual(["fresh", "fresh", "fresh"]);
  expect(fetchMock).toHaveBeenCalledTimes(1); // второй запрос с тем же refresh-токеном получил бы 401
  expect(getAccessToken()).toBe("fresh");
});

it("on 401 refreshes once and retries the request with the new token", async () => {
  setAccessToken("expired");
  const seen: (string | null)[] = [];
  vi.stubGlobal(
    "fetch",
    vi.fn(async (input: Request | string) => {
      if (typeof input === "string") return json({ access_token: "fresh" }); // /auth/refresh
      seen.push(input.headers.get("Authorization"));
      return input.headers.get("Authorization") === "Bearer fresh" ? json([]) : json({ detail: "x" }, 401);
    }),
  );
  const response = await authFetch(new Request("http://panel.test/api/v1/servers"));
  expect(response.status).toBe(200);
  expect(seen).toEqual(["Bearer expired", "Bearer fresh"]);
});

it("gives up after a failed refresh and clears the session", async () => {
  setAccessToken("expired");
  vi.stubGlobal(
    "fetch",
    vi.fn(async (input: Request | string) => (typeof input === "string" ? json({}, 401) : json({}, 401))),
  );
  const response = await authFetch(new Request("http://panel.test/api/v1/servers"));
  expect(response.status).toBe(401);
  expect(getAccessToken()).toBeNull();
});
