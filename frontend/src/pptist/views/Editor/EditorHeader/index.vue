<template>
  <div class="editor-header">
    <div class="left">
      <div class="title">
        <Input
          class="title-input"
          ref="titleInputRef"
          v-model:value="titleValue"
          @blur="handleUpdateTitle()"
          v-if="editingTitle"
        ></Input>
        <div
          class="title-text"
          @click="startEditTitle()"
          :title="title"
          v-else
        >{{ title }}</div>
      </div>
    </div>

    <CanvasTool class="header-canvas-tool" />

    <div class="right">
      <div class="menu-item paper-save" v-if="hasPaperHost" v-tooltip="tr('pptist.saveToPaper')" @click="saveToPaperHost()">
        <i-icon-park-outline:save class="icon" />
        <span class="save-text">{{ savingToPaper ? tr('pptist.saving') : tr('pptist.save') }}</span>
      </div>

      <div class="group-menu-item">
        <div class="menu-item" v-tooltip="tr('pptist.slideshow')" @click="enterScreening()">
          <i-icon-park-outline:ppt class="icon" />
        </div>
        <Popover trigger="click" center>
          <template #content>
            <PopoverMenuItem class="popover-menu-item" @click="enterScreeningFromStart()"><i-icon-park-outline:slide-two class="icon" /> {{ tr('pptist.fromStart') }}</PopoverMenuItem>
            <PopoverMenuItem class="popover-menu-item" @click="enterScreening()"><i-icon-park-outline:ppt class="icon" /> {{ tr('pptist.fromCurrent') }}</PopoverMenuItem>
          </template>
          <div class="arrow-btn"><i-icon-park-outline:down class="arrow" /></div>
        </Popover>
      </div>

      <div class="menu-divider" />

      <div
        v-if="hasConfirmImport || paperDownloadUrl || hasReexport || hasDeleteRun"
        class="result-export-menu paper-export-menu"
        ref="exportMenuRef"
      >
        <div class="result-export-split">
          <button
            v-if="hasConfirmImport"
            type="button"
            class="result-export-main"
            :disabled="confirmImportDisabled || confirmingImport"
            :title="confirmImportHint"
            @click="confirmImportFromPaperHost()"
          >
            <i-icon-park-outline:upload class="icon" />
            <span>{{ confirmingImport ? tr('pptist.importingTemplate') : tr('pptist.importTemplate') }}</span>
          </button>
          <a v-else-if="paperDownloadUrl" class="result-export-main" :href="paperDownloadUrl">
            <i-icon-park-outline:download class="icon" />
            <span>{{ tr('pptist.downloadPptx') }}</span>
          </a>
          <button v-else type="button" class="result-export-main" disabled>
            <i-icon-park-outline:download class="icon" />
            <span>{{ tr('pptist.waitingOutput') }}</span>
          </button>
          <button
            v-if="hasCancelImport || (!hasConfirmImport && (paperDownloadUrl || hasReexport || hasDeleteRun))"
            type="button"
            class="result-export-caret"
            :aria-expanded="exportMenuVisible"
            :aria-label="tr('pptist.downloadOptions')"
            @click="exportMenuVisible = !exportMenuVisible"
          >
            <i-icon-park-outline:down />
          </button>
        </div>
        <div
          class="result-export-menu-content paper-export-menu-content"
          v-if="exportMenuVisible && (hasCancelImport || (!hasConfirmImport && (paperDownloadUrl || hasReexport || hasDeleteRun)))"
        >
          <a v-if="!hasConfirmImport && paperDownloadUrl" :href="paperDownloadUrl" @click="exportMenuVisible = false">
            <i-icon-park-outline:download class="icon" />
            <span>{{ tr('pptist.downloadLatest') }}</span>
          </a>
          <button type="button" v-if="!hasConfirmImport && hasReexport" @click="reexportFromPaperHost()">
            <i-icon-park-outline:refresh class="icon" />
            <span>{{ tr('pptist.reexport') }}</span>
          </button>
          <button type="button" v-if="hasCancelImport" @click="cancelImportFromPaperHost()">
            <i-icon-park-outline:close class="icon" />
            <span>{{ tr('pptist.cancelImport') }}</span>
          </button>
          <button
            type="button"
            v-if="!hasConfirmImport && hasDeleteRun"
            class="paper-export-delete-item"
            :disabled="deletingToPaper"
            @click="deleteRunFromPaperHost()"
          >
            <i-icon-park-outline:delete class="icon" />
            <span>{{ deletingToPaper ? tr('pptist.deleting') : tr('pptist.delete') }}</span>
          </button>
        </div>
      </div>

      <div
        class="menu-item toolbar-toggle"
        :class="{ active: !toolbarCollapsed }"
        v-tooltip="toolbarCollapsed ? tr('pptist.expandProperties') : tr('pptist.collapseProperties')"
        @click="toggleToolbar()"
      >
        <i-icon-park-outline:preview-open class="icon" v-if="toolbarCollapsed" />
        <i-icon-park-outline:preview-close class="icon" v-else />
        <span class="tool-text">{{ tr('pptist.properties') }}</span>
      </div>

      <a
        class="menu-item github-link"
        v-tooltip="tr('pptist.byPptist')"
        href="https://github.com/pipipi-pikachu/PPTist"
        target="_blank"
        rel="noreferrer"
      >
        <i-icon-park-outline:github class="icon" />
      </a>
    </div>
  </div>
</template>

<script lang="ts" setup>
import { nextTick, onBeforeUnmount, onMounted, ref, useTemplateRef } from 'vue'
import { storeToRefs } from 'pinia'
import { useMainStore, useSlidesStore } from '@/store'
import useScreening from '@/hooks/useScreening'

import Input from '@/components/Input.vue'
import Popover from '@/components/Popover.vue'
import PopoverMenuItem from '@/components/PopoverMenuItem.vue'
import CanvasTool from '../CanvasTool/index.vue'
import { pptistT } from '@/i18n'

interface PaperPptistHost {
  save?: () => Promise<void>
  downloadUrl?: string
  confirmImport?: () => Promise<void> | void
  saveBeforeConfirmImport?: boolean
  confirmImportDisabled?: boolean
  confirmImportHint?: string
  cancelImport?: () => void
  reexportPptx?: () => Promise<void> | void
  deleteRun?: () => Promise<void> | void
}

const mainStore = useMainStore()
const slidesStore = useSlidesStore()
const { toolbarCollapsed } = storeToRefs(mainStore)
const { title } = storeToRefs(slidesStore)
const { enterScreening, enterScreeningFromStart } = useScreening()

const editingTitle = ref(false)
const titleValue = ref('')
const titleInputRef = useTemplateRef<InstanceType<typeof Input>>('titleInputRef')
const hasPaperHost = ref(false)
const hasConfirmImport = ref(false)
const hasCancelImport = ref(false)
const confirmImportDisabled = ref(false)
const confirmImportHint = ref('')
const confirmingImport = ref(false)
const hasReexport = ref(false)
const hasDeleteRun = ref(false)
const paperDownloadUrl = ref('')
const savingToPaper = ref(false)
const deletingToPaper = ref(false)
const exportMenuVisible = ref(false)
const exportMenuRef = ref<HTMLElement | null>(null)
const tr = pptistT

const readPaperHost = (): PaperPptistHost => (window as any).__PAPER_PPTIST_HOST__ || {}

const toggleToolbar = () => {
  mainStore.setToolbarCollapsed(!toolbarCollapsed.value)
}

const startEditTitle = () => {
  titleValue.value = title.value
  editingTitle.value = true
  nextTick(() => titleInputRef.value?.focus())
}

const handleUpdateTitle = () => {
  slidesStore.setTitle(titleValue.value)
  editingTitle.value = false
}

const saveToPaperHost = async () => {
  const host = readPaperHost()
  if (!host.save || savingToPaper.value) return
  savingToPaper.value = true
  try {
    await host.save()
  }
  finally {
    savingToPaper.value = false
  }
}

const reexportFromPaperHost = async () => {
  const host = readPaperHost()
  if (!host.reexportPptx) return
  exportMenuVisible.value = false
  await host.reexportPptx()
}

const confirmImportFromPaperHost = async () => {
  const host = readPaperHost()
  if (!host.confirmImport || confirmingImport.value || confirmImportDisabled.value) return
  confirmingImport.value = true
  try {
    if (host.saveBeforeConfirmImport) {
      await host.save?.()
    }
    await host.confirmImport()
  }
  finally {
    confirmingImport.value = false
  }
}

const cancelImportFromPaperHost = () => {
  exportMenuVisible.value = false
  readPaperHost().cancelImport?.()
}

const closeExportMenuOnOutside = (event: PointerEvent) => {
  if (!exportMenuVisible.value) return
  const target = event.target
  if (target instanceof Node && exportMenuRef.value?.contains(target)) return
  exportMenuVisible.value = false
}

const deleteRunFromPaperHost = async () => {
  const host = readPaperHost()
  if (!host.deleteRun || deletingToPaper.value) return
  const confirmed = window.confirm(tr('pptist.deleteConfirm'))
  if (!confirmed) return
  deletingToPaper.value = true
  try {
    await host.deleteRun()
  }
  finally {
    deletingToPaper.value = false
  }
}

onMounted(() => {
  const host = readPaperHost()
  hasPaperHost.value = !!host.save
  hasConfirmImport.value = !!host.confirmImport
  hasCancelImport.value = !!host.cancelImport
  confirmImportDisabled.value = !!host.confirmImportDisabled
  confirmImportHint.value = host.confirmImportHint || ''
  hasReexport.value = !!host.reexportPptx
  hasDeleteRun.value = !!host.deleteRun
  paperDownloadUrl.value = host.downloadUrl || ''
  document.addEventListener('pointerdown', closeExportMenuOnOutside)
})

onBeforeUnmount(() => {
  document.removeEventListener('pointerdown', closeExportMenuOnOutside)
})
</script>

<style lang="scss" scoped>
.editor-header {
  background-color: #fff;
  user-select: none;
  border-bottom: 1px solid $borderColor;
  display: flex;
  justify-content: space-between;
  align-items: center;
  gap: 0;
  padding: 0 8px 0 0;
}
.left,
.right {
  display: flex;
  align-items: center;
}
.left {
  width: 160px;
  height: 100%;
  min-width: 160px;
  justify-content: flex-start;
  border-right: 1px solid $borderColor;
}
.right {
  flex-shrink: 0;
  gap: 4px;
}
.header-canvas-tool {
  height: 100%;
  min-width: 300px;
  flex: 1 1 auto;
  border-bottom: 0;
  border-right: 1px solid $borderColor;
}
.menu-item {
  height: 30px;
  display: flex;
  justify-content: center;
  align-items: center;
  font-size: 14px;
  padding: 0 10px;
  border-radius: $borderRadius;
  cursor: pointer;
  color: #20242a;

  .icon {
    font-size: 18px;
    color: #4f5968;
  }
  .tool-text {
    margin-left: 5px;
    font-size: 13px;
  }

  &.active,
  &:hover {
    background-color: #f1f1f1;
  }
}
.header-link {
  text-decoration: none;
}
.github-link {
  text-decoration: none;
}
.paper-export-menu {
  flex-shrink: 0;
  margin-right: 2px;

  .result-export-main {
    min-height: 30px;
    padding: 0 12px;
    gap: 6px;
    font-size: 13px;
  }

  .result-export-caret {
    width: 32px;
    min-height: 30px;
  }
}
.paper-export-menu-content {
  right: 0;
  top: calc(100% + 6px);
}
.paper-export-delete-item {
  color: #b42318 !important;
}
.paper-export-menu .icon {
  font-size: 16px;
}
.paper-save {
  gap: 6px;
  color: #334155;

  .save-text {
    font-size: 13px;
    line-height: 1;
  }
}
.popover-menu-item {
  display: flex;
  padding: 8px 10px;

  .icon {
    font-size: 18px;
    margin-right: 10px;
  }
}
.group-menu-item {
  height: 30px;
  display: flex;
  margin: 0 2px;
  padding: 0 2px;
  border-radius: $borderRadius;

  &:hover {
    background-color: #f1f1f1;
  }

  .menu-item {
    padding: 0 3px;
  }
  .arrow-btn {
    display: flex;
    justify-content: center;
    align-items: center;
    cursor: pointer;
  }
}
.menu-divider {
  width: 1px;
  height: 18px;
  margin: 0 3px;
  background: $borderColor;
}
.title {
  width: 100%;
  height: 100%;
  min-width: 0;
  display: flex;
  align-items: center;
  justify-content: flex-start;
  padding-left: 28px;

  .title-input {
    width: 124px;
    height: 30px;
    padding-left: 0;
    padding-right: 0;

    ::v-deep(input) {
      height: 28px;
      line-height: 28px;
    }
  }
  .title-text {
    min-width: 20px;
    max-width: 124px;
    line-height: 30px;
    padding: 0 4px;
    border-radius: $borderRadius;
    color: #0f172a;
    font-size: 13px;
    font-weight: 600;
    cursor: pointer;

    @include ellipsis-oneline();

    &:hover {
      background-color: #f1f1f1;
    }
  }
}

@media screen and (width <= 1500px) {
  .editor-header {
    padding-right: 6px;
  }
  .left {
    width: 136px;
    min-width: 136px;
  }
  .title {
    padding-left: 16px;

    .title-input,
    .title-text {
      max-width: 104px;
    }

    .title-input {
      width: 104px;
    }
  }
  .header-canvas-tool {
    min-width: 0;
    flex: 1 1 0;
  }
  .right {
    gap: 2px;
  }
  .menu-item {
    padding: 0 7px;
  }
  .group-menu-item {
    margin: 0 1px;
    padding: 0 1px;
  }
  .menu-divider {
    margin: 0 2px;
  }
  .paper-save .save-text,
  .toolbar-toggle .tool-text,
  .github-link {
    display: none;
  }
  .paper-export-menu .result-export-main {
    padding: 0 10px;
  }
  .paper-export-menu .result-export-caret {
    width: 30px;
  }
}

@media screen and (width <= 1200px) {
  .paper-export-menu .result-export-main span,
  .toolbar-toggle .tool-text,
  .paper-save .save-text {
    display: none;
  }
  .title .title-text {
    max-width: 112px;
  }
  .header-canvas-tool {
    min-width: 220px;
  }
}
</style>
