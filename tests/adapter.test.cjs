const assert = require('node:assert/strict');
const { readFileSync } = require('node:fs');
const { test } = require('node:test');
const vm = require('node:vm');
const source = readFileSync('custom_components/ingress/www/unifi-adapter.js', 'utf8');

function browser() {
  class Element {
    setAttribute(name, value) { this[name] = value; }
    hasAttribute() { return false; }
    querySelectorAll() { return []; }
  }
  class Node {
    appendChild(node) { return node; }
    insertBefore(node) { return node; }
    replaceChild(node) { return node; }
  }
  class WebSocket {
    constructor(url, protocols) { this.url = url; this.protocols = protocols; }
    send() {}
  }
  class XMLHttpRequest { open(method, url) { this.url = url; } }
  const location = new URL('https://ha.example/api/ingress/app/');
  const window = {
    location, WebSocket,
    fetch: (url) => url,
    open: (url) => ({ location: { href: url } }),
  };
  const document = {
    currentScript: { dataset: {
      ingressPath: '/api/ingress/app',
      upstreamOrigin: 'https://backend.example:8006',
      ingressLinks: Buffer.from(JSON.stringify({
        'http://backend.example:8384/': '/api/ingress/syncthing/',
      })).toString('base64'),
    } },
    addEventListener() {},
  };
  const context = { window, document, URL, Request, Element, Node,
    XMLHttpRequest, atob, history: { pushState() {}, replaceState() {} } };
  for (const [name, property] of [
    ['HTMLImageElement', 'src'], ['HTMLIFrameElement', 'src'],
    ['HTMLScriptElement', 'src'], ['HTMLLinkElement', 'href'],
    ['HTMLSourceElement', 'src'], ['HTMLVideoElement', 'poster'],
    ['HTMLAnchorElement', 'href'], ['HTMLFormElement', 'action'],
  ]) {
    const type = class extends Element {};
    Object.defineProperty(type.prototype, property, {
      configurable: true, get() { return this.value; }, set(value) { this.value = value; },
    });
    context[name] = type;
  }
  vm.runInNewContext(source, context);
  return { ...context, NativeWebSocket: WebSocket };
}

test('noVNC receives the original WebSocket prototype and signed URL', () => {
  const { window, NativeWebSocket } = browser();
  const ws = new window.WebSocket('wss://backend.example:8006/ws?ticket=a%2Bb%2Fc%3D', ['binary']);
  assert.equal(Object.getPrototypeOf(ws), NativeWebSocket.prototype);
  assert.equal(ws.send, NativeWebSocket.prototype.send);
  assert.equal(ws.url, 'wss://ha.example/api/ingress/app/ws?ticket=a%2Bb%2Fc%3D');
  assert.deepEqual(ws.protocols, ['binary']);
});

test('dynamic iframe and delayed console popup stay inside ingress', () => {
  const { window, HTMLIFrameElement } = browser();
  const iframe = new HTMLIFrameElement();
  iframe.src = '/novnc/vnc.html';
  assert.equal(iframe.src, '/api/ingress/app/novnc/vnc.html');
  const popup = window.open('');
  popup.location.href = '?console=kvm&vmid=100';
  assert.equal(popup.location.href, '/api/ingress/app/?console=kvm&vmid=100');
});

test('Docker child links use the configured route and external links remain external', () => {
  const { window } = browser();
  assert.equal(window.fetch('http://backend.example:8384/settings'), '/api/ingress/syncthing/settings');
  assert.equal(window.fetch('https://external.example/login'), 'https://external.example/login');
  assert.equal(window.fetch('/api/ingress/app/api/ingress/app/api'), '/api/ingress/app/api');
  assert.equal(window.fetch('syncthing/view.html'), 'syncthing/view.html');
});
