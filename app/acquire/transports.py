"""Transport registry — maps Transport enum values to implementations."""
from .spec import Transport
from .transport_ftp import FtpTransport
from .transport_hf_hub import HfHubTransport
from .transport_https import HttpsTransport
from .transport_rsync import RsyncTransport
from .transport_torrent import TorrentTransport

REGISTRY = {
    Transport.HTTPS: HttpsTransport(),
    Transport.FTP: FtpTransport(),
    Transport.RSYNC: RsyncTransport(),
    Transport.HF_HUB: HfHubTransport(),
    Transport.TORRENT: TorrentTransport(),
}
