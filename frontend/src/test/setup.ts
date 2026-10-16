import "@testing-library/jest-dom/vitest";
import { afterEach, beforeEach, vi } from "vitest";
import { cleanup } from "@testing-library/react";

afterEach(() => { cleanup(); vi.restoreAllMocks(); });
const matchMedia = vi.fn();
const defaultMatchMedia = (query: string) => ({ matches: false, media: query, onchange: null, addListener: vi.fn(), removeListener: vi.fn(), addEventListener: vi.fn(), removeEventListener: vi.fn(), dispatchEvent: vi.fn() });
Object.defineProperty(window, "matchMedia", { writable: true, value: matchMedia });
const storage = new Map<string,string>();
const localStorageMock: Storage = { get length(){return storage.size}, clear:()=>storage.clear(), getItem:(key)=>storage.get(key)??null, key:(index)=>[...storage.keys()][index]??null, removeItem:(key)=>void storage.delete(key), setItem:(key,value)=>void storage.set(key,String(value)) };
Object.defineProperty(globalThis,"localStorage",{configurable:true,value:localStorageMock});
beforeEach(() => { matchMedia.mockImplementation(defaultMatchMedia); window.localStorage.clear(); });
class DOMMatrixReadOnlyMock {
  a=1;b=0;c=0;d=1;e=0;f=0;m11=1;m12=0;m13=0;m14=0;m21=0;m22=1;m23=0;m24=0;m31=0;m32=0;m33=1;m34=0;m41=0;m42=0;m43=0;m44=1;
  constructor(transform?:string){const match=transform?.match(/matrix\(([^)]+)\)/);if(match){const values=match[1].split(",").map(Number);this.a=this.m11=values[0]??1;this.d=this.m22=values[3]??1;this.e=this.m41=values[4]??0;this.f=this.m42=values[5]??0}}
}
Object.defineProperty(window,"DOMMatrixReadOnly",{writable:true,value:DOMMatrixReadOnlyMock});
class ResizeObserverMock {
  constructor(private readonly callback: ResizeObserverCallback) {}
  observe(target: Element) {
    const contentRect = DOMRect.fromRect({ width: 800, height: 400 });
    this.callback([{ target, contentRect } as ResizeObserverEntry], this as unknown as ResizeObserver);
  }
  unobserve() {}
  disconnect() {}
}
globalThis.ResizeObserver = ResizeObserverMock;
class IntersectionObserverMock {
  root = null; rootMargin = "0px"; thresholds = [0];
  constructor() {}
  observe() {} unobserve() {} disconnect() {} takeRecords() { return []; }
}
globalThis.IntersectionObserver = IntersectionObserverMock;
Object.assign(navigator, { clipboard: { writeText: vi.fn().mockResolvedValue(undefined) } });
URL.createObjectURL = vi.fn(() => "blob:fixture");
URL.revokeObjectURL = vi.fn();
