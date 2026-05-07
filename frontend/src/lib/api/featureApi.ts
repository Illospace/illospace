import { api } from './client';

type ApiClient = typeof api;
type ApiFunction = (...args: any[]) => any;

export type ApiFunctionName = {
  [Name in keyof ApiClient]: ApiClient[Name] extends ApiFunction ? Name : never;
}[keyof ApiClient];

export type PickedApiMethods<Names extends readonly ApiFunctionName[]> = {
  [Name in Names[number]]: ApiClient[Name];
};

export function pickApiMethods<const Names extends readonly ApiFunctionName[]>(
  names: Names,
): PickedApiMethods<Names> {
  return Object.fromEntries(names.map((name) => [name, api[name]])) as PickedApiMethods<Names>;
}

export type TypedApiMethods = Partial<Record<ApiFunctionName, ApiFunction>>;

export function pickTypedApiMethods<Methods extends TypedApiMethods>(
  names: readonly (keyof Methods & ApiFunctionName)[],
): Methods {
  return pickApiMethods(names as readonly ApiFunctionName[]) as unknown as Methods;
}
