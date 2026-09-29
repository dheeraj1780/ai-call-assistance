using System.Text;
using System.Windows.Forms;
using CallCopilot.GatewaySecretHandoff;

namespace GatewaySecretHandoff.Tests;

// DUMMY values only. Ports the 16 PC-side checks of the PowerShell generator, plus clipboard behaviour.
public class GeneratorTests
{
    private static readonly List<string> Many = Enumerable.Range(0, 2000).Select(_ => SecretGenerator.NewSecret()).ToList();

    [Fact] public void Always_64_characters() => Assert.All(Many, s => Assert.Equal(64, s.Length));

    [Fact] public void Url_safe_charset_only() =>
        Assert.All(Many, s => Assert.Matches("^[A-Za-z0-9_-]{64}$", s));

    [Fact] public void Values_are_distinct() => Assert.Equal(2000, Many.Distinct(StringComparer.Ordinal).Count());

    [Fact] public void Decodes_back_to_48_bytes() =>
        Assert.Equal(48, Convert.FromBase64String(Many[0].Replace('-', '+').Replace('_', '/')).Length);
}

public class EnvFileTests
{
    private const string K = "TEAMS_MEDIA_GATEWAY_SECRET";
    private static readonly string Dummy = "DUMMY-" + new string('x', 58);
    private const string Crlf = "APP_ENV=development\r\n# comment line\r\n\r\nDATABASE_URL=postgresql://u:p@h/db\r\nPUBLIC_BASE_URL=https://example.invalid\r\n";
    private const string Lf = "A=1\nTEAMS_MEDIA_GATEWAY_SECRET=OLDVALUE_OLDVALUE_OLDVALUE_OLDVALUE\nB=2";

    [Fact] public void Append_adds_the_key_line_once() =>
        Assert.Single(EnvFile.Update(Crlf, K, () => Dummy, false).Split("\r\n"), l => l == $"{K}={Dummy}");

    [Fact] public void Append_keeps_all_original_lines_in_order_including_comment_and_blank() =>
        Assert.Equal(EnvFile.LinesExcept(Crlf, K), EnvFile.LinesExcept(EnvFile.Update(Crlf, K, () => Dummy, false), K));

    [Fact] public void Append_preserves_crlf_without_bare_lf() =>
        Assert.DoesNotContain("\n", EnvFile.Update(Crlf, K, () => Dummy, false).Replace("\r\n", ""));

    [Fact] public void Append_ends_with_exactly_one_newline()
    {
        var o = EnvFile.Update(Crlf, K, () => Dummy, false);
        Assert.EndsWith("\r\n", o);
        Assert.False(o.EndsWith("\r\n\r\n", StringComparison.Ordinal));
    }

    [Fact] public void Existing_key_without_replace_is_refused() =>
        Assert.Contains("already exists", Assert.Throws<EnvFileException>(() => EnvFile.Update(Lf, K, () => Dummy, false)).Message);

    [Fact] public void Replace_is_in_place_and_keeps_lf()
    {
        var o = EnvFile.Update(Lf, K, () => Dummy, true);
        Assert.Equal($"{K}={Dummy}", o.Split('\n')[1]);
        Assert.DoesNotContain("\r", o);
    }

    [Fact] public void Replace_removes_the_old_value() =>
        Assert.DoesNotContain("OLDVALUE", EnvFile.Update(Lf, K, () => Dummy, true));

    [Fact] public void Replace_keeps_other_lines_unchanged() =>
        Assert.Equal(EnvFile.LinesExcept(Lf, K), EnvFile.LinesExcept(EnvFile.Update(Lf, K, () => Dummy, true), K));

    [Fact] public void Duplicate_key_is_refused() =>
        Assert.Contains("appears 2 times", Assert.Throws<EnvFileException>(() => EnvFile.Update($"{K}=a\n{K}=b\n", K, () => Dummy, true)).Message);

    [Fact] public void Similar_key_is_untouched() =>
        Assert.StartsWith("TEAMS_MEDIA_GATEWAY_URL=https://x\n", EnvFile.Update("TEAMS_MEDIA_GATEWAY_URL=https://x\n", K, () => Dummy, false));

    [Fact] public void Empty_file_gets_a_single_line() => Assert.Equal("K=v\n", EnvFile.Update("", "K", () => "v", false));

    [Fact] public void Atomic_replace_keeps_bom_and_leaves_no_temp_file()
    {
        var dir = Directory.CreateTempSubdirectory("gsh-test-");
        try
        {
            var target = Path.Combine(dir.FullName, ".env");
            File.WriteAllBytes(target, EnvFile.Encode("A=1\r\n", bom: true));
            EnvFile.AtomicReplace(target, EnvFile.Encode("A=1\r\nK=v\r\n", bom: true));
            var (text, bom) = EnvFile.Decode(File.ReadAllBytes(target));
            Assert.True(bom);
            Assert.Equal("A=1\r\nK=v\r\n", text);
            Assert.Single(Directory.GetFiles(dir.FullName));
        }
        finally { dir.Delete(recursive: true); }
    }

    // ---- additional checks ----
    [Fact] public void Bom_round_trip_and_no_bom()
    {
        Assert.True(EnvFile.Decode(EnvFile.Encode("x", true)).Bom);
        Assert.False(EnvFile.Decode(EnvFile.Encode("x", false)).Bom);
        Assert.Equal("x", EnvFile.Decode(EnvFile.Encode("x", true)).Text);
    }

    [Fact] public void Key_state_reports_count_and_match()
    {
        var o = EnvFile.Update(Crlf, K, () => Dummy, false);
        Assert.Equal((1, true), EnvFile.KeyState(o, K, () => Dummy));
        Assert.Equal((1, false), EnvFile.KeyState(o, K, () => "other"));
    }

    [Fact] public void Error_messages_never_contain_the_value()
    {
        var messages = new[]
        {
            Assert.Throws<EnvFileException>(() => EnvFile.Update(Lf, K, () => Dummy, false)).Message,
            Assert.Throws<EnvFileException>(() => EnvFile.Update($"{K}={Dummy}\n{K}={Dummy}\n", K, () => Dummy, true)).Message,
        };
        Assert.All(messages, m => Assert.DoesNotContain(Dummy, m));
    }
}

public class ClipboardTests
{
    private static readonly string Dummy = "CLIPDUMMY-" + Guid.NewGuid().ToString("N") + "-not-a-secret";

    private static void Sta(Action body)
    {
        Exception? error = null;
        var t = new Thread(() => { try { body(); } catch (Exception ex) { error = ex; } });
        t.SetApartmentState(ApartmentState.STA);
        t.Start(); t.Join();
        if (error is not null) throw new Xunit.Sdk.XunitException("STA body failed: " + error);
    }

    [Fact]
    public void Data_object_carries_text_and_history_cloud_exclusion_formats() => Sta(() =>
    {
        var data = ClipboardHandoff.Build(() => Dummy);
        Assert.Equal(Dummy, data.GetData(DataFormats.UnicodeText));
        foreach (var format in new[] { ClipboardHandoff.HistoryFormat, ClipboardHandoff.CloudFormat })
        {
            Assert.True(data.GetDataPresent(format), format);
            var stream = Assert.IsType<MemoryStream>(data.GetData(format));
            Assert.Equal(new byte[] { 0, 0, 0, 0 }, stream.ToArray());
        }
    });

    [Fact]
    public void Hand_off_clears_the_clipboard_after_waiting() => Sta(() =>
    {
        var sawIt = false;
        var clean = ClipboardHandoff.HandOff(() => Dummy, () => sawIt = ClipboardHandoff.Holds(() => Dummy));
        Assert.True(sawIt);
        Assert.True(clean);
        Assert.False(ClipboardHandoff.Holds(() => Dummy));
    });

    [Fact]
    public void Hand_off_clears_the_clipboard_when_the_wait_fails() => Sta(() =>
    {
        Assert.Throws<InvalidOperationException>(() => ClipboardHandoff.HandOff(() => Dummy, () => throw new InvalidOperationException("simulated")));
        Assert.False(ClipboardHandoff.Holds(() => Dummy));
    });

    [Fact]
    public void Clear_leaves_unrelated_clipboard_content_alone() => Sta(() =>
    {
        Clipboard.SetText("unrelated-" + Guid.NewGuid().ToString("N"));
        Assert.True(ClipboardHandoff.ClearIfHolds(() => Dummy));
        Assert.True(Clipboard.ContainsText());
        Clipboard.Clear();
    });
}
