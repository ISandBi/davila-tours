export default async (request) => {
  const backendValue = Netlify.env.get("BACKEND_URL");
  if (!backendValue) {
    return new Response("Configura BACKEND_URL en las variables de Netlify.", {
      status: 503,
      headers: { "cache-control": "no-store" },
    });
  }

  const backend = new URL(backendValue);
  const isLocalBackend =
    backend.hostname === "localhost" || backend.hostname === "127.0.0.1";
  if (
    (backend.protocol !== "https:" &&
      !(isLocalBackend && backend.protocol === "http:")) ||
    backend.username ||
    backend.password ||
    backend.pathname !== "/" ||
    backend.search ||
    backend.hash
  ) {
    return new Response("BACKEND_URL debe ser el origen HTTPS del backend.", {
      status: 500,
      headers: { "cache-control": "no-store" },
    });
  }

  const incoming = new URL(request.url);
  const destination = new URL(
    `${incoming.pathname}${incoming.search}`,
    backend.origin,
  );
  const headers = new Headers(request.headers);
  headers.delete("host");
  headers.delete("connection");
  headers.set("x-forwarded-host", incoming.host);
  headers.set("x-forwarded-proto", incoming.protocol.slice(0, -1));

  const init = {
    method: request.method,
    headers,
    redirect: "manual",
  };
  if (request.method !== "GET" && request.method !== "HEAD") {
    init.body = await request.arrayBuffer();
  }

  const upstream = await fetch(new Request(destination, init));
  const responseHeaders = new Headers(upstream.headers);
  responseHeaders.set("cache-control", "private, no-store");

  const location = responseHeaders.get("location");
  if (location) {
    const redirect = new URL(location, backend.origin);
    if (redirect.origin === backend.origin) {
      redirect.protocol = incoming.protocol;
      redirect.host = incoming.host;
      responseHeaders.set("location", redirect.toString());
    }
  }

  return new Response(upstream.body, {
    status: upstream.status,
    statusText: upstream.statusText,
    headers: responseHeaders,
  });
};

export const config = { path: "/*" };
