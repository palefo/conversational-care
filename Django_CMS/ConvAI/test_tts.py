"""Text to speech: one switch, two providers.

*Nothing set means ElevenLabs, as before.* Azure is chosen, never fallen into.

*A voice belongs to a provider.* Each agent keeps one per provider, and the
reply is spoken in the one that matches the provider that is on.

*A reply is model output.* It is escaped into the SSML, never trusted as markup.

    python3 manage.py test ConvAI.test_tts --settings=test_settings
"""
import os
import shutil
import tempfile
from unittest.mock import MagicMock, patch

import requests
from django.core.cache import cache
from django.test import TestCase, override_settings

from ConvAI import tts
from ConvAI.forms import AgentConfigForm, NativeAgentForm, PromptAgentForm
from ConvAI.models import Agent, SiteConfiguration

# Every TTS variable cleared, so the container's own .env cannot decide a test.
_CLEAN_ENV = {k: "" for k in (
    "TTS_PROVIDER", "AZURE_SPEECH_KEY", "AZURE_SPEECH_REGION", "AZURE_SPEECH_VOICE",
    "ELEVENLABS_API_KEY", "ELEVENLABS_VOICE_ID", "LINK_WORKER_AZURE_VOICE_PT_BR",
)}


def _config(**kwargs):
    cfg = SiteConfiguration.load()
    for k, v in kwargs.items():
        setattr(cfg, k, v)
    cfg.save()
    cache.clear()


def _azure(**extra):
    _config(**{"tts_provider": "azure", "azure_speech_key": "test-key-not-real",
               "azure_speech_region": "uksouth", **extra})


def _ok(content=b"ID3fake-mp3"):
    return MagicMock(status_code=200, content=content)


class Base(TestCase):
    def setUp(self):
        self._env = patch.dict(os.environ, _CLEAN_ENV)
        self._env.start()
        cache.clear()
        self.dir = tempfile.mkdtemp()
        self._dir = override_settings(VOICE_RECORDINGS_DIR=self.dir)
        self._dir.enable()

    def tearDown(self):
        self._dir.disable()
        shutil.rmtree(self.dir, ignore_errors=True)
        self._env.stop()
        cache.clear()


class ProviderTests(Base):
    def test_nothing_set_means_elevenlabs(self):
        self.assertEqual(tts.provider(), tts.ELEVENLABS)

    def test_env_chooses_azure(self):
        with patch.dict(os.environ, {"TTS_PROVIDER": "Azure"}):
            self.assertEqual(tts.provider(), tts.AZURE)

    def test_settings_win_over_env(self):
        _config(tts_provider="elevenlabs")
        with patch.dict(os.environ, {"TTS_PROVIDER": "azure"}):
            self.assertEqual(tts.provider(), tts.ELEVENLABS)

    def test_an_unknown_provider_is_elevenlabs(self):
        with patch.dict(os.environ, {"TTS_PROVIDER": "polly"}):
            self.assertEqual(tts.provider(), tts.ELEVENLABS)


class VoiceTests(Base):
    def setUp(self):
        super().setUp()
        self.agent = Agent.objects.create(name="A", kind="native", native_key="loopback",
                                          tts_voice_id="elevenVoice", azure_voice="pt-BR-AntonioNeural")
        self.bare = Agent.objects.create(name="B", kind="native", native_key="loopback")

    def test_elevenlabs_uses_the_agents_elevenlabs_voice(self):
        self.assertEqual(tts.voice_for_agent(self.agent), "elevenVoice")
        self.assertEqual(self.agent.active_tts_voice, "elevenVoice")

    def test_elevenlabs_default_voice_comes_from_settings(self):
        _config(elevenlabs_voice_id="savedVoice")
        self.assertEqual(tts.voice_for_agent(self.bare), "savedVoice")

    def test_azure_uses_the_agents_azure_voice(self):
        _azure()
        self.assertEqual(tts.voice_for_agent(self.agent), "pt-BR-AntonioNeural")
        self.assertEqual(self.agent.active_tts_voice, "pt-BR-AntonioNeural")

    def test_azure_without_an_agent_voice_uses_the_default(self):
        _azure()
        self.assertEqual(tts.voice_for_agent(self.bare), tts.DEFAULT_AZURE_VOICE)
        self.assertEqual(self.bare.active_tts_voice, "")
        _azure(azure_speech_voice="en-GB-SoniaNeural")
        self.assertEqual(tts.voice_for_agent(self.bare), "en-GB-SoniaNeural")

    def test_an_elevenlabs_voice_is_never_sent_to_azure(self):
        _azure()
        only_eleven = Agent.objects.create(name="C", kind="native", native_key="loopback",
                                           tts_voice_id="elevenVoice")
        self.assertEqual(tts.voice_for_agent(only_eleven), tts.DEFAULT_AZURE_VOICE)


class AzureSynthesisTests(Base):
    def test_speaks_into_the_recordings_dir(self):
        _azure()
        with patch("ConvAI.tts.requests.post", return_value=_ok()) as post:
            path = tts.synthesize_speech("Olá, **Maria**!", "out.mp3", voice="pt-BR-FranciscaNeural")
        self.assertEqual(path, os.path.join(self.dir, "out.mp3"))
        with open(path, "rb") as fh:
            self.assertEqual(fh.read(), b"ID3fake-mp3")
        url = post.call_args.args[0]
        kw = post.call_args.kwargs
        self.assertEqual(url, "https://uksouth.tts.speech.microsoft.com/cognitiveservices/v1")
        self.assertEqual(kw["headers"]["Ocp-Apim-Subscription-Key"], "test-key-not-real")
        self.assertEqual(kw["headers"]["X-Microsoft-OutputFormat"], tts.AZURE_OUTPUT_FORMAT)
        body = kw["data"].decode("utf-8")
        self.assertIn('xml:lang="pt-BR"', body)
        self.assertIn('<voice name="pt-BR-FranciscaNeural">Olá, Maria!</voice>', body)

    def test_the_agents_voice_is_used(self):
        _azure()
        agent = Agent.objects.create(name="A", kind="native", native_key="loopback",
                                     azure_voice="en-GB-RyanNeural")
        with patch("ConvAI.tts.requests.post", return_value=_ok()) as post:
            tts.synthesize_speech("Hello", "out.mp3", agent=agent)
        self.assertIn('name="en-GB-RyanNeural"', post.call_args.kwargs["data"].decode())

    def test_the_reply_is_escaped_not_trusted_as_markup(self):
        _azure()
        with patch("ConvAI.tts.requests.post", return_value=_ok()) as post:
            tts.synthesize_speech('a < b & </voice><voice name="x">c', "out.mp3")
        body = post.call_args.kwargs["data"].decode()
        self.assertEqual(body.count("<voice "), 1)
        self.assertIn("a &lt; b &amp; &lt;/voice&gt;", body)

    def test_a_pasted_endpoint_is_read_for_its_region(self):
        _azure(azure_speech_region="https://uksouth.api.cognitive.microsoft.com/")
        self.assertEqual(tts.azure_region(), "uksouth")
        _azure(azure_speech_region="uk south/../evil")
        self.assertEqual(tts.azure_region(), "")

    def test_not_configured_is_an_error_and_no_request(self):
        _config(tts_provider="azure")
        with patch("ConvAI.tts.requests.post") as post, self.assertRaises(tts.TTSError):
            tts.synthesize_speech("Hello", "out.mp3")
        post.assert_not_called()

    def test_a_refusal_is_an_error_and_leaves_no_file(self):
        _azure()
        with patch("ConvAI.tts.requests.post", return_value=MagicMock(status_code=401, content=b"")), \
                self.assertRaisesRegex(tts.TTSError, "401"):
            tts.synthesize_speech("Hello", "out.mp3")
        self.assertFalse(os.path.exists(os.path.join(self.dir, "out.mp3")))

    def test_unreachable_is_an_error(self):
        _azure()
        with patch("ConvAI.tts.requests.post", side_effect=requests.ConnectionError("down")), \
                self.assertRaises(tts.TTSError):
            tts.synthesize_speech("Hello", "out.mp3")


class ElevenLabsPathTests(Base):
    def test_elevenlabs_still_speaks_by_default(self):
        agent = Agent.objects.create(name="A", kind="native", native_key="loopback",
                                     tts_voice_id="elevenVoice")
        with patch("ConvAI.utils.synthesize_speech_elevenlabs", return_value="/x") as eleven, \
                patch("ConvAI.tts.requests.post") as post:
            tts.synthesize_speech("Hello", "out.mp3", agent=agent)
        eleven.assert_called_once_with("Hello", "out.mp3", voice_id="elevenVoice")
        post.assert_not_called()

    def test_the_saved_elevenlabs_key_is_used(self):
        _config(elevenlabs_api_key="saved-key-not-real")
        with patch("elevenlabs.client.ElevenLabs") as client, patch("elevenlabs.save"):
            from ConvAI.utils import synthesize_speech_elevenlabs
            synthesize_speech_elevenlabs("Hello", "out.mp3", voice_id="v")
        self.assertEqual(client.call_args.kwargs.get("api_key"), "saved-key-not-real")


class VoiceListTests(Base):
    def test_without_azure_the_common_voices_are_offered(self):
        with patch("ConvAI.tts.requests.get") as get:
            self.assertEqual(tts.azure_voices(), tts.COMMON_AZURE_VOICES)
        get.assert_not_called()

    def test_the_regions_voices_are_listed_and_cached(self):
        _azure()
        payload = [
            {"ShortName": "pt-BR-FranciscaNeural", "DisplayName": "Francisca",
             "LocaleName": "Portuguese (Brazil)", "Gender": "Female", "SecondaryLocaleList": []},
            {"ShortName": "en-GB-AdaMultilingualNeural", "DisplayName": "Ada",
             "LocaleName": "English (United Kingdom)", "Gender": "Female",
             "SecondaryLocaleList": ["pt-BR"]},
        ]
        resp = MagicMock(status_code=200)
        resp.json.return_value = payload
        with patch("ConvAI.tts.requests.get", return_value=resp) as get:
            first, second = tts.azure_voices(), tts.azure_voices()
        get.assert_called_once()
        self.assertEqual(first, second)
        self.assertEqual(first[0], ("en-GB-AdaMultilingualNeural",
                                    "Ada — English (United Kingdom), female, multilingual"))
        self.assertEqual(first[1], ("pt-BR-FranciscaNeural", "Francisca — Portuguese (Brazil), female"))

    def test_a_failed_list_falls_back_quietly(self):
        _azure()
        with patch("ConvAI.tts.requests.get", side_effect=requests.Timeout()), \
                self.assertLogs("ConvAI.tts", level="WARNING"):
            self.assertEqual(tts.azure_voices(), tts.COMMON_AZURE_VOICES)


class FormTests(Base):
    def test_settings_save_the_tts_fields_and_keep_the_key(self):
        cfg = SiteConfiguration.load()
        data = {f: getattr(cfg, f) or "" for f in AgentConfigForm.Meta.fields}
        data.update(tts_provider="azure", azure_speech_key="first-key-not-real",
                    azure_speech_region="uksouth", azure_speech_voice="en-GB-SoniaNeural",
                    link_worker_azure_voice_pt_br="pt-BR-ThalitaMultilingualNeural")
        form = AgentConfigForm(data, instance=cfg)
        self.assertTrue(form.is_valid(), form.errors)
        form.save()
        cfg = SiteConfiguration.load()
        data["azure_speech_key"] = ""  # write-only: blank keeps it
        form = AgentConfigForm(data, instance=cfg)
        self.assertTrue(form.is_valid(), form.errors)
        form.save()
        cfg = SiteConfiguration.load()
        self.assertEqual((cfg.tts_provider, cfg.azure_speech_key, cfg.azure_speech_voice),
                         ("azure", "first-key-not-real", "en-GB-SoniaNeural"))
        self.assertNotIn("first-key-not-real", str(AgentConfigForm(instance=cfg)["azure_speech_key"]))

    def test_agent_forms_offer_the_azure_voice_with_suggestions(self):
        for form_cls in (NativeAgentForm, PromptAgentForm):
            field = form_cls()["azure_voice"]
            self.assertIn('list="azure-voices"', str(field))
            self.assertEqual(field.label, "Azure voice")
