(() => {
  "use strict";

  const currentScript = document.currentScript;
  const nativeLocation = window.location;
  const ingressPath = currentScript?.dataset.ingressPath?.replace(/\/$/, "");
  const upstreamOrigin = currentScript?.dataset.upstreamOrigin;
  let ingressLinks = {};
  try {
    ingressLinks = JSON.parse(atob(currentScript?.dataset.ingressLinks || "e30="));
  } catch (_error) {}
  if (!ingressPath) return;

  const ingressPrefix = `${ingressPath}/`;
  const rewrite = (value) => {
    if (typeof value !== "string") return value;
    // Relative URLs already resolve below the current ingress path. They can
    // also be Angular expressions (for example, 'syncthing/view.html'), so
    // interpreting them as URLs corrupts templates and dependency names.
    if (!value.startsWith("/") && !/^[a-z][a-z\d+.-]*:\/\//i.test(value)) {
      return value;
    }
    try {
      const rootRelative = value.startsWith("/") && !value.startsWith("//");
      const url = new URL(value, nativeLocation.href);
      const doubledPrefix = `${ingressPath}${ingressPrefix}`;
      let normalized = false;
      while (url.pathname.startsWith(doubledPrefix)) {
        url.pathname = `${ingressPrefix}${url.pathname.slice(doubledPrefix.length)}`;
        normalized = true;
      }
      for (const [source, target] of Object.entries(ingressLinks)) {
        const sourceUrl = new URL(source);
        const sourcePath = sourceUrl.pathname.replace(/\/$/, "");
        if (
          url.origin === sourceUrl.origin &&
          (url.pathname === sourcePath || url.pathname.startsWith(`${sourcePath}/`))
        ) {
          const remainder = url.pathname.slice(sourcePath.length).replace(/^\//, "");
          return `${target}${remainder}${url.search}${url.hash}`;
        }
      }
      if (url.host === nativeLocation.host && url.pathname.startsWith("/files/ingress/")) {
        return value;
      }
      const proxyHost = url.host === nativeLocation.host;
      const upstreamHost = url.host === new URL(upstreamOrigin).host;
      if (!proxyHost && !upstreamHost) {
        return value;
      }
      if (proxyHost && url.pathname.startsWith(ingressPrefix)) {
        return normalized ? `${url.pathname}${url.search}${url.hash}` : value;
      }
      const websocket = ["ws:", "wss:"].includes(url.protocol);
      url.protocol = websocket
        ? nativeLocation.protocol === "https:"
          ? "wss:"
          : "ws:"
        : nativeLocation.protocol;
      url.hostname = nativeLocation.hostname;
      url.port = nativeLocation.port;
      url.pathname = `${ingressPath}${url.pathname}`;
      if (rootRelative && !websocket) {
        return `${url.pathname}${url.search}${url.hash}`;
      }
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

  const nativeOpen = window.open.bind(window);
  window.open = (url, ...rest) => {
    const opened = nativeOpen(url == null ? url : rewrite(String(url)), ...rest);
    if (opened === null) return null;
    return new Proxy(opened, {
      get(target, property) {
        const value = Reflect.get(target, property);
        return typeof value === "function" ? value.bind(target) : value;
      },
      set(target, property, value) {
        if (property === "location") {
          target.location = rewrite(String(value));
          return true;
        }
        return Reflect.set(target, property, value);
      },
    });
  };

  const rewriteNode = (node) => {
    const rewriteElement = (element) => {
      for (const attribute of ["src", "href", "action", "poster"]) {
        if (element.hasAttribute(attribute)) {
          element.setAttribute(attribute, rewrite(element.getAttribute(attribute)));
        }
      }
    };
    if (node instanceof Element) rewriteElement(node);
    if (typeof node?.querySelectorAll === "function") {
      for (const element of node.querySelectorAll("[src],[href],[action],[poster]")) {
        rewriteElement(element);
      }
    }
  };

  const nativeSetAttribute = Element.prototype.setAttribute;
  Element.prototype.setAttribute = function (name, value) {
    const normalizedName = String(name).toLowerCase();
    if (["src", "href", "action", "poster"].includes(normalizedName)) {
      value = rewrite(String(value));
    }
    return nativeSetAttribute.call(this, name, value);
  };

  for (const [elementType, property] of [
    [HTMLImageElement, "src"],
    [HTMLScriptElement, "src"],
    [HTMLLinkElement, "href"],
    [HTMLSourceElement, "src"],
    [HTMLVideoElement, "poster"],
    [HTMLAnchorElement, "href"],
    [HTMLFormElement, "action"],
  ]) {
    const descriptor = Object.getOwnPropertyDescriptor(elementType.prototype, property);
    if (!descriptor?.get || !descriptor?.set) continue;
    Object.defineProperty(elementType.prototype, property, {
      configurable: descriptor.configurable,
      enumerable: descriptor.enumerable,
      get: descriptor.get,
      set(value) {
        descriptor.set.call(this, rewrite(String(value)));
      },
    });
  }

  for (const method of ["appendChild", "insertBefore", "replaceChild"]) {
    const nativeMethod = Node.prototype[method];
    Node.prototype[method] = function (node, ...rest) {
      rewriteNode(node);
      return nativeMethod.call(this, node, ...rest);
    };
  }

  const mutationRoot = document.documentElement;
  if (mutationRoot) {
    new MutationObserver((records) => {
      for (const record of records) {
        for (const node of record.addedNodes) rewriteNode(node);
      }
    }).observe(mutationRoot, { childList: true, subtree: true });
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
