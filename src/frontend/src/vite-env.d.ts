/// <reference types="vite/client" />

interface ImportMetaEnv {
  readonly VITE_DEV_FIXTURES?: string;
}

interface ImportMeta {
  readonly env: ImportMetaEnv;
}
