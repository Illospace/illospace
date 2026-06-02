export type SignalBlobPointerUpAction = 'activate' | 'suppress-click' | 'none';

export interface SignalBlobPointerUpState {
  dragMoved: boolean;
  pointerMoved: boolean;
  canActivate: boolean;
}

export function signalBlobPointerUpAction({
  dragMoved,
  pointerMoved,
  canActivate,
}: SignalBlobPointerUpState): SignalBlobPointerUpAction {
  if (dragMoved || pointerMoved) return 'suppress-click';
  if (canActivate) return 'activate';
  return 'none';
}
