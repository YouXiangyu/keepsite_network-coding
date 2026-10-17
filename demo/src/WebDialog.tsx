import type { PropsWithChildren } from 'react';
import * as Dialog from '@radix-ui/react-dialog';
import { X } from '@phosphor-icons/react';

export default function WebDialog({ open, onOpenChange, title, children }: PropsWithChildren<{
  open: boolean; onOpenChange: (open: boolean) => void; title: string;
}>) {
  return <Dialog.Root open={open} onOpenChange={onOpenChange}>
    <Dialog.Portal><Dialog.Overlay className="dialog-overlay" />
      <Dialog.Content className="web-dialog" aria-describedby={undefined}>
        <header className="dialog-header"><Dialog.Title>{title}</Dialog.Title>
          <Dialog.Close className="icon-button" aria-label="关闭对话框"><X size={22} /></Dialog.Close>
        </header><div className="dialog-body">{children}</div>
      </Dialog.Content>
    </Dialog.Portal>
  </Dialog.Root>;
}
