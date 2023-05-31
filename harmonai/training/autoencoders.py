import torch
import torchaudio
import wandb
from einops import rearrange
from torch import nn, optim
from torch.nn import functional as F
import auraloss
import pytorch_lightning as pl
from ..models.autoencoders import AudioAutoencoder
from ..models.discriminators import EncodecDiscriminator

from pytorch_lightning.utilities.distributed import rank_zero_only
from aeiou.viz import pca_point_cloud, audio_spectrogram_image, tokens_spectrogram_image

class AutoencoderTrainingWrapper(pl.LightningModule):
    def __init__(
            self, 
            autoencoder: AudioAutoencoder,
            lr: float = 1e-4,
            warmup_steps: int = 150000,
            sample_rate=48000
    ):
        super().__init__()

        self.automatic_optimization = False

        self.autoencoder = autoencoder

        self.warmed_up = False
        self.warmup_steps = warmup_steps
        self.lr = lr
        
        scales = [2048, 1024, 512]
        hop_sizes = []
        win_lengths = []
        overlap = 0.75
        for s in scales:
            hop_sizes.append(int(s * (1 - overlap)))
            win_lengths.append(s)

        self.sdstft = auraloss.freq.SumAndDifferenceSTFTLoss(fft_sizes=scales, hop_sizes=hop_sizes, win_lengths=win_lengths, sample_rate=sample_rate, scale="mel", n_bins=64)

        self.discriminator = EncodecDiscriminator(
            filters=64,
            in_channels = 2,
            out_channels = 1,
            n_ffts = [2048, 1024, 512, 256, 128],
            hop_lengths = [512, 256, 128, 64, 32],
            win_lengths = [2048, 1024, 512, 256, 128]
        )

    def configure_optimizers(self):
        opt_gen = optim.Adam([*self.autoencoder.parameters()], lr=self.lr, betas=(.5, .9))
        opt_disc = optim.Adam([*self.discriminator.parameters()], lr=self.lr, betas=(.5, .9))
        return [opt_gen, opt_disc]
  
    def training_step(self, batch, batch_idx):
        reals, _ = batch

        # Remove extra dimension added by WebDataset
        if reals.ndim == 4 and reals.shape[0] == 1:
            reals = reals[0]

        if self.global_step >= self.warmup_steps:
            self.warmed_up = True

        opt_gen, opt_disc = self.optimizers()

        latents, encoder_info = self.autoencoder.encode(reals, return_info=True)

        decoded = self.autoencoder.decode(latents)

        mrstft_loss = self.sdstft(reals, decoded)

        l1_time_loss = F.l1_loss(reals, decoded)

        if self.warmed_up:
            loss_dis, loss_adv, feature_matching_distance, _, _ = self.discriminator.loss(reals, decoded)
        else:
            loss_dis = torch.tensor(0.).to(reals)
            loss_adv = torch.tensor(0.).to(reals)
            feature_matching_distance = torch.tensor(0.).to(reals)

        # Train the discriminator
        if self.global_step % 2 and self.warmed_up:
            loss = loss_dis

            log_dict = {
                'train/discriminator_loss': loss_dis.detach()  
            }

            opt_disc.zero_grad()
            self.manual_backward(loss_dis)
            opt_disc.step()

        # Train the generator 
        else:

            loss_adv = 0.1 * loss_adv

            feature_matching_distance =  10 * feature_matching_distance

            # Combine spectral loss, KL loss, time-domain loss, and adversarial loss
            loss = mrstft_loss + loss_adv + feature_matching_distance

            if self.autoencoder.bottleneck == "vae":
                kl = encoder_info['kl']
                kl_loss = 1e-3 * kl 
                loss = loss + kl_loss

            opt_gen.zero_grad()
            self.manual_backward(loss)
            opt_gen.step()

            log_dict = {
                'train/loss': loss.detach(),
                'train/mrstft_loss': mrstft_loss.detach(),   
                'train/l1_time_loss': l1_time_loss.detach(),
                'train/loss_adv': loss_adv.detach(),
                'train/feature_matching': feature_matching_distance.detach(),
                'train/latent_std': latents.std().detach(),
            }

            if self.autoencoder.bottleneck == "vae":
                log_dict['train/kl_loss'] = kl_loss.detach()
            
        self.log_dict(log_dict, prog_bar=True, on_step=True)

        return loss


class AutoencoderDemoCallback(pl.Callback):
    def __init__(
        self, 
        demo_dl, 
        demo_every=2000,
        sample_size=65536,
        sample_rate=48000
    ):
        super().__init__()
        self.demo_every = demo_every
        self.demo_samples = sample_size
        self.demo_dl = iter(demo_dl)
        self.sample_rate = sample_rate
        self.last_demo_step = -1

    @rank_zero_only
    @torch.no_grad()
    def on_train_batch_end(self, trainer, module, outputs, batch, batch_idx): 
        if (trainer.global_step - 1) % self.demo_every != 0 or self.last_demo_step == trainer.global_step:
            return
        
        self.last_demo_step = trainer.global_step

        try:
            demo_reals, _ = next(self.demo_dl)

            encoder_input = demo_reals
            
            encoder_input = encoder_input.to(module.device)

            demo_reals = demo_reals.to(module.device)

            with torch.no_grad():
                latents = module.autoencoder.encode(encoder_input)

                fakes = module.autoencoder.decode(latents)

            # Put the demos together
            fakes = rearrange(fakes, 'b d n -> d (b n)')
            demo_reals = rearrange(demo_reals, 'b d n -> d (b n)')

            log_dict = {}
            
            filename = f'recon_{trainer.global_step:08}.wav'
            fakes = fakes.clamp(-1, 1).mul(32767).to(torch.int16).cpu()
            torchaudio.save(filename, fakes, self.sample_rate)

            reals_filename = f'reals_{trainer.global_step:08}.wav'
            demo_reals = demo_reals.clamp(-1, 1).mul(32767).to(torch.int16).cpu()
            torchaudio.save(reals_filename, demo_reals, self.sample_rate)


            log_dict[f'recon'] = wandb.Audio(filename,
                                                sample_rate=self.sample_rate,
                                                caption=f'Reconstructed')
            log_dict[f'real'] = wandb.Audio(reals_filename,
                                                sample_rate=self.sample_rate,
                                                caption=f'Real')

            log_dict[f'embeddings_3dpca'] = pca_point_cloud(latents)
            log_dict[f'embeddings_spec'] = wandb.Image(tokens_spectrogram_image(latents))

            log_dict[f'real_melspec_left'] = wandb.Image(audio_spectrogram_image(demo_reals))
            log_dict[f'recon_melspec_left'] = wandb.Image(audio_spectrogram_image(fakes))

            trainer.logger.experiment.log(log_dict, step=trainer.global_step)
        except Exception as e:
            print(f'{type(e).__name__}: {e}')