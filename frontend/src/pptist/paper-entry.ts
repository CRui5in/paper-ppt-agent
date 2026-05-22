import 'prosemirror-view/style/prosemirror.css'
import 'animate.css'
import '@/assets/styles/prosemirror.scss'
import '@/assets/styles/global.scss'
import '@/assets/styles/font.scss'

import { createApp, type App } from 'vue'
import { createPinia } from 'pinia'
import Directive from '@/directive'
import PaperApp from './PaperApp.vue'

export type PptistStudioSource =
  | { kind: 'preview'; jobId: string; revision?: string }
  | { kind: 'templateImport'; importId: string; revision?: string }

export interface PptistStudioController {
  save: () => Promise<void>
  exportPptx: () => Promise<void>
  destroy: () => void
}

export interface PptistStudioMountOptions {
  source: PptistStudioSource
  locale?: 'en' | 'zh'
  downloadHref?: string
  onConfirmImport?: () => Promise<void> | void
  saveBeforeConfirmImport?: boolean
  confirmImportDisabled?: boolean
  confirmImportHint?: string
  onCancelImport?: () => void
  onReexport?: () => void
  onDeleteRun?: () => Promise<void> | void
  onStatus?: (message: string) => void
  onSaved?: (result: unknown) => void
  onError?: (message: string) => void
}

export function mountPptistStudio(
  container: HTMLElement,
  options: PptistStudioMountOptions,
): PptistStudioController {
  const controller: PptistStudioController = {
    save: async () => undefined,
    exportPptx: async () => undefined,
    destroy: () => undefined,
  }
  const app = createApp(PaperApp, {
    options: {
      kind: options.source.kind,
      id: options.source.kind === 'preview' ? options.source.jobId : options.source.importId,
      locale: options.locale,
      controller,
      downloadUrl: options.downloadHref,
      onConfirmImport: options.onConfirmImport,
      saveBeforeConfirmImport: options.saveBeforeConfirmImport,
      confirmImportDisabled: options.confirmImportDisabled,
      confirmImportHint: options.confirmImportHint,
      onCancelImport: options.onCancelImport,
      onReexport: options.onReexport,
      onDeleteRun: options.onDeleteRun,
      onStatus: options.onStatus,
      onSaved: options.onSaved,
      onError: options.onError,
    },
  })

  app.use(createPinia())
  app.use(Directive)
  app.mount(container)
  controller.destroy = () => {
    app.unmount()
    container.innerHTML = ''
  }
  return controller
}

export type MountedPptistStudio = ReturnType<typeof mountPptistStudio>
