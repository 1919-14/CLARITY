import { useEffect, useRef } from 'react';

/**
 * Hook to manage focus trapping, Escape key closing, and focus restoration
 * for accessible modal dialogs.
 *
 * @param {Object} options
 * @param {boolean} options.open Whether the modal is open
 * @param {Function} options.onClose Callback invoked when Escape key is pressed
 * @param {boolean} [options.busy] If true, Escape key closing is suppressed
 * @returns {React.RefObject} Ref to attach to the dialog container element
 */
export function useModalFocusTrap({ open, onClose, busy = false }) {
  const containerRef = useRef(null);
  const previousFocusRef = useRef(null);

  useEffect(() => {
    if (!open) return undefined;

    previousFocusRef.current = document.activeElement;

    // Focus the first focusable control inside the modal or the modal container itself
    const timer = setTimeout(() => {
      if (!containerRef.current) return;
      const focusables = Array.from(
        containerRef.current.querySelectorAll(
          'button:not([disabled]), [href], input:not([disabled]), select:not([disabled]), textarea:not([disabled]), [tabindex]:not([tabindex="-1"])',
        ),
      );
      if (focusables.length > 0) {
        focusables[0].focus();
      } else {
        containerRef.current.focus?.();
      }
    }, 20);

    const handleKeyDown = (event) => {
      if (event.key === 'Escape' && !busy) {
        event.preventDefault();
        onClose?.();
        return;
      }

      if (event.key === 'Tab' && containerRef.current) {
        const focusables = Array.from(
          containerRef.current.querySelectorAll(
            'button:not([disabled]), [href], input:not([disabled]), select:not([disabled]), textarea:not([disabled]), [tabindex]:not([tabindex="-1"])',
          ),
        );
        if (focusables.length === 0) return;

        const first = focusables[0];
        const last = focusables[focusables.length - 1];

        if (event.shiftKey && document.activeElement === first) {
          event.preventDefault();
          last.focus();
        } else if (!event.shiftKey && document.activeElement === last) {
          event.preventDefault();
          first.focus();
        }
      }
    };

    document.addEventListener('keydown', handleKeyDown);

    return () => {
      clearTimeout(timer);
      document.removeEventListener('keydown', handleKeyDown);
      const previous = previousFocusRef.current;
      if (previous && typeof previous.focus === 'function') {
        previous.focus();
      }
    };
  }, [open, busy, onClose]);

  return containerRef;
}
