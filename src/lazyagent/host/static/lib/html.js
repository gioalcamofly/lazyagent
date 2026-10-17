// One place to import the view library from: Preact, htm bound to it, the
// signals (whose import also installs their Preact integration) and a
// shallow-props memo.

import { h, render, Component, Fragment } from "../vendor/preact/preact.mjs";
import htm from "../vendor/preact/htm.mjs";
import { signal, computed, effect, batch } from "../vendor/preact/signals.mjs";

export const html = htm.bind(h);
export { h, render, Component, Fragment, signal, computed, effect, batch };

function sameProps(a, b) {
  for (const key in a) if (a[key] !== b[key]) return false;
  for (const key in b) if (!(key in a)) return false;
  return true;
}

/** A function component that re-renders only when a prop changes identity. */
export function memo(fn) {
  class Memo extends Component {
    shouldComponentUpdate(props) {
      return !sameProps(props, this.props);
    }
    render(props) {
      return fn(props);
    }
  }
  Memo.displayName = `memo(${fn.name})`;
  return Memo;
}
