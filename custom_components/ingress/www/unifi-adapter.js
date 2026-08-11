(() => {
  "use strict";

  const currentScript = document.currentScript;
  const nativeLocation = window.location;
  const ingressPath = currentScript?.dataset.ingressPath?.replace(/\/$/, "");
  const upstreamOrigin = currentScript?.dataset.upstreamOrigin;
  if (!ingressPath) return;

  const ingressPrefix = `${ingressPath}/`;
  const rewrite = (value) => {
    if (typeof value !== "string") return value;
    try {
      const url = new URL(value, nativeLocation.href);
      const proxyHost = url.host === nativeLocation.host;
      const upstreamHost = url.host === new URL(upstreamOrigin).host;
      if ((!proxyHost && !upstreamHost) || (proxyHost && url.pathname.startsWith(ingressPrefix))) {
        return value;
      }
      const websocket = ["ws:", "wss:"].includes(url.protocol);
      url.protocol = websocket
        ? nativeLocation.protocol === "https:"
          ? "wss:"
          : "ws:"
        : nativeLocation.protocol;
      url.host = nativeLocation.host;
      url.pathname = `${ingressPath}${url.pathname}`;
      return url.href;
    } catch (_error) {
      return value;
    }
  };

  const nativeFetch = window.fetch.bind(window);
  window.fetch = (input, init) => {
    if (input instanceof Request) input = new Request(rewrite(input.url), input);
    else input = rewrite(input);
    return nativeFetch(input, init);
  };

  const nativeXhrOpen = XMLHttpRequest.prototype.open;
  XMLHttpRequest.prototype.open = function (method, url, ...rest) {
    return nativeXhrOpen.call(this, method, rewrite(url), ...rest);
  };

  const NativeWebSocket = window.WebSocket;
  window.WebSocket = class extends NativeWebSocket {
    constructor(url, protocols) {
      if (protocols === undefined) super(rewrite(url));
      else super(rewrite(url), protocols);
    }
  };

  const rewriteNode = (node) => {
    if (!(node instanceof Element)) return;
    for (const attribute of ["src", "href", "action"]) {
      if (node.hasAttribute(attribute)) {
        node.setAttribute(attribute, rewrite(node.getAttribute(attribute)));
      }
    }
  };
  for (const method of ["appendChild", "insertBefore", "replaceChild"]) {
    const nativeMethod = Node.prototype[method];
    Node.prototype[method] = function (node, ...rest) {
      rewriteNode(node);
      return nativeMethod.call(this, node, ...rest);
    };
  }

  for (const method of ["pushState", "replaceState"]) {
    const nativeMethod = history[method];
    history[method] = function (state, unused, url) {
      return nativeMethod.call(
        this,
        state,
        unused,
        url == null ? url : rewrite(String(url)),
      );
    };
  }

  window.__HA_INGRESS_LOCATION__ = {
    get pathname() {
      const path = nativeLocation.pathname;
      if (path === ingressPath) return "/";
      return path.startsWith(ingressPrefix) ? path.slice(ingressPath.length) : path;
    },
    get search() {
      return nativeLocation.search;
    },
    get hash() {
      return nativeLocation.hash;
    },
    get origin() {
      return upstreamOrigin;
    },
    get host() {
      return new URL(upstreamOrigin).host;
    },
    get hostname() {
      return new URL(upstreamOrigin).hostname;
    },
    get port() {
      return new URL(upstreamOrigin).port;
    },
    get protocol() {
      return new URL(upstreamOrigin).protocol;
    },
    get href() {
      return `${upstreamOrigin}${this.pathname}${this.search}${this.hash}`;
    },
    set href(value) {
      nativeLocation.href = rewrite(String(value));
    },
    assign(value) {
      nativeLocation.assign(rewrite(String(value)));
    },
    replace(value) {
      nativeLocation.replace(rewrite(String(value)));
    },
    reload() {
      nativeLocation.reload();
    },
    toString() {
      return this.href;
    },
  };
  window.__HA_INGRESS_PATH__ = ingressPath;
})();
