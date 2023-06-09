import asyncio
import logging
import zmq
import zmq.asyncio
import json
import numpy as np
from lib.generator_hex.sound_alsa_pb2.SaState import PlayBack
from .errata import *
from .contact import *

logger = logging.getLogger(__name__)


async def set_feeder(duration, **kwargs):
    logger.debug("Setting feed duration")
    param_set = await Request.spawn(request_type="SetParameters",
                                    component='stepper-motor',
                                    body={'timeout': duration}
                                    )
    await param_set.send()

    interval_check = await Request.spawn(request_type="GetParameters",
                                         component='stepper-motor',
                                         body=None)
    check_res = await interval_check.send()
    if check_res.timeout != duration:
        logger.error(f"Stepper motor timeout parameter not set to {duration}")


async def feed(duration, **kwargs):
    logger.debug('feed() called, requesting stepper motor')
    start = asyncio.create_task(
        catch('stepper-motor',
              caught=lambda pub: pub.running,
              failure=lambda i: pub_err("stepper-motor") if not i else None,
              timeout=TIMEOUT,
              **kwargs)
    )
    req = await Request.spawn(request_type="ChangeState",
                              component='stepper-motor',
                              body={'running': True, 'direction': True})
    await req.send()
    await start
    logger.debug('feeding confirmed by decide-rs, awaiting motor stop')
    await catch('stepper-motor',
                caught=lambda pub: not pub.running,
                failure=lambda i: pub_err("stepper-motor") if not i else None,
                timeout=duration + TIMEOUT,
                **kwargs)
    logger.debug('motor stop confirmed by decide-rs')
    return


async def cue(pos, color):
    logger.debug('Requesting cue led')
    asyncio.create_task(
        catch(pos,
              caught=lambda pub: pub.led_state == color,
              failure=lambda i: pub_err(pos) if not i else None,
              timeout=TIMEOUT)
    )
    req = await Request.spawn(request_type="ChangeState",
                              component=pos,
                              body={'led_state': color})
    await req.send()
    return


class Sun:
    @classmethod
    async def spawn(cls, interval):
        logger.debug("Sun spawning")
        self = Sun()
        self.brightness = 0
        self.daytime = False
        self.interval = interval
        return self

    async def cycle(self, **kwargs):
        logger.debug("Sun cycle initiating")
        param_set = await Request.spawn(request_type="SetParameters",
                                        component='house-light',
                                        body={'clock_interval': self.interval}
                                        )
        await param_set.send()

        interval_check = await Request.spawn(request_type="GetParameters",
                                             component='house-light',
                                             body=None)
        check_res = await interval_check.send()
        if check_res.clock_interval != self.interval:
            logger.error(f"House-Light Clock Interval not set to {self.interval},"
                         f" got {check_res.clock_interval}")

        def light_update(msg):
            self.brightness = msg.brightness
            self.daytime = msg.daytime
            return True

        while True:
            logger.debug('new house light cycle, awaiting update')
            await catch('house-light',
                        caught=light_update,
                        failure=lambda i: pub_err("house-light") if not i else None,
                        timeout=self.interval + 1)


async def blip(brightness, interval):
    ctx = zmq.asyncio.Context.instance()
    logger.debug("Manually changing house lights")
    asyncio.create_task(
        catch('house-light',
              caught=lambda pub: True if pub.brightness == brightness else False,
              failure=lambda i: pub_err("house-light") if not i else None,
              timeout=100)
    )
    req = await Request.spawn(request_type="ChangeState",
                              component='house-light',
                              body={'manual': True, 'brightness': brightness}
                              )
    await req.send()
    logger.debug("Manually changing house lights confirmed by decide-rs.")
    await asyncio.sleep(interval)
    logger.debug("Returning house lights to cycle")
    asyncio.create_task(
        catch('house-light',
              lambda pub: not pub.manual,
              lambda i: pub_err("house-light") if not i else None,
              timeout=100)
    )
    req2 = await Request.spawn(request_type="ChangeState",
                               component='house-light',
                               body={'manual': False, 'ephemera': True}
                               )
    await req2.send()
    logger.debug("Returning house lights to cycle succeeded")
    return


class JukeBox:
    def __init__(self):
        self.stim_duration = None
        self.playing = None
        self.stim_data = None
        self.sample_rate = None

    @classmethod
    async def spawn(cls, conf_fs, shuffle=True):
        logger.info("Spawning Playback Machine")
        self = JukeBox()
        with open(conf_fs) as file:
            cf = json.load(file)
            self.dir = cf['stimulus_root']
            self.stim_data = cf['stimuli']

        self.cue_locations = {}
        playlist = []

        logger.debug("Validating and generating playlist")
        for i, stim in enumerate(self.stim_data):
            cue_loc = None
            for action, consq in stim['responses'].items():
                total = (consq['p_reward'] if 'p_reward' in consq else 0) + \
                        (consq['p_punish'] if 'p_punish' in consq else 0)
                if total > 1:
                    logger.error(f"Reward/Punish Percentage Exceed 1.0 for {action} in {stim['name']}")
                    raise
                if 'p_reward' in consq:
                    cue_loc = action
            self.cue_locations[stim['name']] = cue_loc
            playlist.append(np.repeat(i, stim['frequency']))

        self.playlist = np.array(playlist).flatten()
        if shuffle:
            np.random.shuffle(self.playlist)

        logger.debug("Requesting stimuli directory change")
        params = await Request.spawn(request_type="SetParameters",
                                     component='audio-playback',
                                     body={'audio_dir': cf['stimulus_root']}
                                     )
        await params.send()
        dir_check = await Request.spawn(request_type="GetParameters",
                                        component='audio-playback',
                                        body=None,
                                        )
        # The following has a higher timeout due to the blocking action of stimuli import on decide-rs
        check_res = await dir_check.send(timeout=10000)
        if check_res.audio_dir != cf['stimulus_root']:
            logger.error(f"Auditory folder mismatch: got {check_res.audio_dir} expected {cf['stimulus_root']}")
        self.sample_rate = check_res.sample_rate
        self.stim_duration = None
        return self

    def __iter__(self):
        self.iter = iter(self.playlist)
        self.playing = False
        self.stimulus = None
        return self

    def __next__(self):
        item = next(self.iter)
        self.stimulus = self.stim_data[item]['name']
        return self.stim_data[item]

    async def current_cue(self):
        if self.stimulus is None:
            logger.error("Trying to determine cue but no stimulus specified. Try initiating playlist first")
            raise
        return self.cue_locations[self.stimulus]

    async def play(self, stim=None):

        if stim is None:
            stim = self.stimulus
        logger.debug(f"Playback of {stim} requested")

        play_result = asyncio.create_task(
            catch('audio-playback',
                  caught=lambda pub: (pub.audio_id == stim) & (pub.playback == PlayBack.PLAYING),
                  failure=lambda i: pub_err("audio-playback") if not i else None,
                  timeout=TIMEOUT)
        )
        req = await Request.spawn(request_type="ChangeState",
                                  component='audio-playback',
                                  body={'audio_id': stim, 'playback': PlayBack.PLAYING}
                                  )
        await req.send()
        _, pub, _ = await play_result
        frame_count = pub.frame_count

        self.stim_duration = frame_count/self.sample_rate
        self.playing = True

        completion = asyncio.create_task(
            catch('audio-playback',
                  caught=lambda pub: (pub.playback == PlayBack.STOPPED),
                  failure=lambda i: pub_err("audio-playback") if not i else None,
                  timeout=self.stim_duration+TIMEOUT)
        )
        return self.stim_duration, completion

    async def stop(self, context=None):
        pub_confirmation = asyncio.create_task(
            catch('audio-playback',
                  lambda pub: (pub.playback == PlayBack.STOPPED),
                  failure=lambda i: pub_err("audio-playback") if not i else None,
                  timeout=100)
        )
        req = await Request.spawn(request_type="ChangeState",
                                  component='audio-playback',
                                  body={'playback': PlayBack.STOPPED}
                                  )
        await req.send()
        await pub_confirmation
        self.playing = False
        return
