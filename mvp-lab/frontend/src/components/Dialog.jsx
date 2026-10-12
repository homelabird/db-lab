import { useLayoutEffect, useRef } from 'react';

export function Dialog({ id, labelledBy, onClose, onCancel, children }) {
  const dialog = useRef(null);
  const close = useRef(onClose);
  const openedHash = useRef(location.hash);
  close.current = onClose;

  useLayoutEffect(() => {
    const element = dialog.current;
    const previous = document.activeElement;
    element.showModal();
    const input = element.querySelector('[autofocus], input:not(:disabled):not([type="checkbox"])');
    if (input) {
      const bounds = input.getBoundingClientRect(), viewport = element.getBoundingClientRect();
      if (bounds.top >= viewport.top && bounds.bottom <= viewport.bottom) input.focus({ preventScroll: true });
    }
    return () => {
      element.close();
      if (openedHash.current === location.hash) {
        const available = previous?.isConnected && previous.getClientRects().length && !previous.matches(':disabled');
        if (available) previous.focus({ preventScroll: true });
        if (!available || document.activeElement !== previous) {
          document.querySelector('.view:not([hidden]) h1')?.focus({ preventScroll: true });
        }
      }
    };
  }, []);

  useLayoutEffect(() => {
    if (openedHash.current !== location.hash) {
      dialog.current.close();
      close.current();
    }
  }, [location.hash]);

  return <dialog ref={dialog} id={id} aria-labelledby={labelledBy}
    onClose={event => { if (!event.currentTarget.open) close.current(); }}
    onCancel={event => {
      event.preventDefault();
      if (!onCancel || onCancel(event) !== false) close.current();
    }}>{children}</dialog>;
}
