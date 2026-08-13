import type { RefObject } from "react";

interface VideoPanelProps {
  videoUrl?: string;
  videoRef?: RefObject<HTMLVideoElement | null>;
}

export function VideoPanel({ videoUrl, videoRef }: VideoPanelProps) {
  return (
    <div className="tile" data-tile="video">
      <div className="tile__header">
        <h3>Raw Video</h3>
        <span className="tile__badge tile__badge--real">SAR-RARP50</span>
      </div>
      <div className="tile__body tile__body--center">
        {videoUrl ? (
          <video ref={videoRef} src={videoUrl} controls className="tile__video" />
        ) : (
          <p className="tile__placeholder">No case video loaded — waiting for a case to be opened.</p>
        )}
      </div>
    </div>
  );
}
