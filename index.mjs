import z from '@deepseek-ai/schemastery';
import { createAdapter, DEFAULT_MODELS } from './adapter.mjs';

export const name = 'notion-local-tools';
export const inject = ['llm', 'credentials'];
export const Config = z.object({
  baseURL: z.string().default('').volatile(),
  apiKeyEnv: z.string().role('credential-ref').default('NOTION_LOCAL_TOOLS_API_KEY').volatile(),
  displayName: z.string().default('Notion · 本地工具'),
  models: z.array(z.object({
    id: z.string(),
    name: z.string(),
    contextWindow: z.number().min(1),
    maxTokens: z.number().min(1),
    inputModalities: z.array(z.string()),
  })).default(DEFAULT_MODELS).volatile(),
  timeoutMs: z.number().min(1000).max(600000).default(330000).volatile(),
});

export function apply(ctx, config) {
  const provider = 'notion-local-tools';
  ctx.llm.registerConfigurableProviders([{
    provider,
    displayName: config.displayName,
    settingsNs: ctx.fiber.entry?.options.id ?? 'notion-local-tools-adapter',
    settingsPath: [],
  }]);
  ctx.llm.registerAdapter([provider], createAdapter(ctx, () => ({
    displayName: config.displayName,
    baseURL: config.baseURL.get(),
    apiKeyEnv: config.apiKeyEnv.get(),
    models: config.models.get(),
    timeoutMs: config.timeoutMs.get(),
  })));
}
