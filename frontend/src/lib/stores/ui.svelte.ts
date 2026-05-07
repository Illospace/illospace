interface ToastMsg {
  id: number;
  text: string;
  type: 'info' | 'error' | 'success';
}

class UiStore {
  toasts = $state<ToastMsg[]>([]);
  searchOpen = $state(false);
  private nextId = 0;

  toast(text: string, type: 'info' | 'error' | 'success' = 'info') {
    const id = this.nextId++;
    this.toasts = [...this.toasts, { id, text, type }];
    setTimeout(() => this.dismiss(id), 5000);
  }

  dismiss(id: number) {
    this.toasts = this.toasts.filter((t) => t.id !== id);
  }

  toggleSearch() {
    this.searchOpen = !this.searchOpen;
  }
}

export const ui = new UiStore();
