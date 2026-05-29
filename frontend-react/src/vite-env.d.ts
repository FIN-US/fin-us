/// <reference types="vite/client" />

interface ImportMetaEnv {
  readonly VITE_NAT_API_PREFIX?: string;
}

interface ImportMeta {
  readonly env: ImportMetaEnv;
}
