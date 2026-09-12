import asyncio
import io
from typing import Optional, Tuple

import asyncssh
import discord
from redbot.core import commands, Config, checks
from redbot.core.bot import Red
from redbot.core.data_manager import cog_data_path


class LinuxGSM(commands.Cog):
    """
    Zarządzanie serwerem gier LinuxGSM (start/stop/restart/status)
    przez SSH, bezpośrednio z poziomu Discorda.

    Bot działa w kontenerze, więc łączy się przez SSH z hostem/serwerem
    gdzie działa LinuxGSM, logując się jako użytkownik systemowy
    (np. vhserver), pod którym uruchomiony jest serwer gry.
    """

    def __init__(self, bot: Red):
        self.bot = bot
        self.config = Config.get_conf(self, identifier=987654321123456, force_registration=True)

        default_guild = {
            "script_path": None,     # np. /home/vhserver/csgoserver/csgoserver
            "ssh_host": None,        # adres IP / hostname hosta z LinuxGSM
            "ssh_port": 22,
            "ssh_user": "vhserver",  # użytkownik systemowy na hoście
            "ssh_key_file": None,    # nazwa pliku klucza prywatnego (w folderze danych coga)
            "allowed_roles": [],
            "log_channel": None,
        }
        self.config.register_guild(**default_guild)

        # blokada na serwer/guild, żeby nie odpalać dwóch akcji naraz
        self._locks: dict[int, asyncio.Lock] = {}

    def _get_lock(self, guild_id: int) -> asyncio.Lock:
        if guild_id not in self._locks:
            self._locks[guild_id] = asyncio.Lock()
        return self._locks[guild_id]

    # ---------------------------------------------------------------
    # Pomocnicze funkcje - SSH
    # ---------------------------------------------------------------

    def _keys_dir(self):
        path = cog_data_path(self) / "keys"
        path.mkdir(parents=True, exist_ok=True)
        return path

    async def _run_remote(
        self,
        host: str,
        port: int,
        user: str,
        key_path: Optional[str],
        script_path: str,
        action: str,
    ) -> Tuple[bool, str]:
        """
        Łączy się po SSH z hostem i wykonuje komendę LinuxGSM.
        LinuxGSM wymaga uruchomienia z własnego katalogu (ścieżki względne),
        więc wchodzimy do katalogu skryptu przed jego wywołaniem.
        """
        script_dir = "/".join(script_path.rstrip("/").split("/")[:-1]) or "/"
        script_name = script_path.rstrip("/").split("/")[-1]
        remote_cmd = f'cd "{script_dir}" && ./{script_name} {action}'

        connect_kwargs = {
            "host": host,
            "port": port,
            "username": user,
            "known_hosts": None,  # UWAGA: wyłącza weryfikację known_hosts, patrz uwaga w dokumentacji
        }
        if key_path:
            connect_kwargs["client_keys"] = [key_path]

        try:
            async with asyncssh.connect(**connect_kwargs) as conn:
                result = await asyncio.wait_for(
                    conn.run(remote_cmd, check=False),
                    timeout=120,
                )
        except asyncio.TimeoutError:
            return False, "Przekroczono limit czasu (120s) połączenia SSH lub wykonania komendy."
        except asyncssh.Error as e:
            return False, f"Błąd SSH: {e}"
        except Exception as e:
            return False, f"Nieoczekiwany błąd: {e}"

        output = (result.stdout or "").strip()
        error = (result.stderr or "").strip()

        if result.exit_status != 0:
            return False, error or output or f"Kod wyjścia: {result.exit_status}"

        return True, output or "Wykonano pomyślnie."

    async def _check_permissions(self, ctx: commands.Context) -> bool:
        if await self.bot.is_owner(ctx.author) or ctx.author.guild_permissions.administrator:
            return True

        allowed_roles = await self.config.guild(ctx.guild).allowed_roles()
        if not allowed_roles:
            return False

        user_role_ids = {role.id for role in ctx.author.roles}
        return bool(user_role_ids.intersection(allowed_roles))

    async def _log_action(self, ctx: commands.Context, action: str, success: bool):
        channel_id = await self.config.guild(ctx.guild).log_channel()
        if not channel_id:
            return
        channel = ctx.guild.get_channel(channel_id)
        if not channel:
            return

        color = discord.Color.green() if success else discord.Color.red()
        embed = discord.Embed(
            title="LinuxGSM - Akcja serwera",
            description=(
                f"**Akcja:** {action}\n"
                f"**Wykonał:** {ctx.author.mention}\n"
                f"**Status:** {'✅ Sukces' if success else '❌ Błąd'}"
            ),
            color=color,
        )
        await channel.send(embed=embed)

    # ---------------------------------------------------------------
    # Grupa komend - sterowanie serwerem
    # ---------------------------------------------------------------

    @commands.group(name="lgsm", invoke_without_command=True)
    @commands.guild_only()
    async def lgsm(self, ctx: commands.Context):
        """Komendy zarządzania serwerem LinuxGSM (przez SSH)."""
        await ctx.send_help()

    @lgsm.command(name="start")
    async def lgsm_start(self, ctx: commands.Context):
        """Uruchamia serwer gry."""
        await self._execute(ctx, "start")

    @lgsm.command(name="stop")
    async def lgsm_stop(self, ctx: commands.Context):
        """Zatrzymuje serwer gry."""
        await self._execute(ctx, "stop")

    @lgsm.command(name="restart")
    async def lgsm_restart(self, ctx: commands.Context):
        """Restartuje serwer gry."""
        await self._execute(ctx, "restart")

    @lgsm.command(name="status")
    async def lgsm_status(self, ctx: commands.Context):
        """Sprawdza status serwera gry."""
        await self._execute(ctx, "details", is_status=True)

    async def _execute(self, ctx: commands.Context, action: str, is_status: bool = False):
        if not await self._check_permissions(ctx):
            await ctx.send("❌ Nie masz uprawnień do zarządzania serwerem.")
            return

        conf = await self.config.guild(ctx.guild).all()
        script_path = conf["script_path"]
        host = conf["ssh_host"]
        port = conf["ssh_port"]
        user = conf["ssh_user"]
        key_file = conf["ssh_key_file"]

        missing = []
        if not script_path:
            missing.append(f"`{ctx.clean_prefix}lgsmset path <ścieżka>`")
        if not host:
            missing.append(f"`{ctx.clean_prefix}lgsmset host <adres>`")
        if missing:
            await ctx.send("⚠️ Brakuje konfiguracji:\n" + "\n".join(missing))
            return

        key_path = str(self._keys_dir() / key_file) if key_file else None
        if key_file and not (self._keys_dir() / key_file).exists():
            await ctx.send(
                "⚠️ Plik klucza SSH nie istnieje w folderze danych coga. "
                f"Wgraj go komendą `{ctx.clean_prefix}lgsmset uploadkey` (załącz plik klucza)."
            )
            return

        lock = self._get_lock(ctx.guild.id)
        if lock.locked() and not is_status:
            await ctx.send("⏳ Inna akcja jest już w trakcie wykonywania, poczekaj chwilę.")
            return

        async def _do():
            async with ctx.typing():
                return await self._run_remote(host, port, user, key_path, script_path, action)

        if is_status:
            success, output = await _do()
        else:
            async with lock:
                success, output = await _do()

        if not is_status:
            await self._log_action(ctx, action, success)

        if len(output) > 1500:
            output = output[-1500:]

        embed = discord.Embed(
            title=f"LinuxGSM - {action}",
            description=f"```{output}```" if output else "Brak danych wyjściowych.",
            color=discord.Color.green() if success else discord.Color.red(),
        )
        await ctx.send(embed=embed)

    # ---------------------------------------------------------------
    # Konfiguracja
    # ---------------------------------------------------------------

    @commands.group(name="lgsmset")
    @commands.guild_only()
    @checks.admin_or_permissions(administrator=True)
    async def lgsmset(self, ctx: commands.Context):
        """Konfiguracja pluginu LinuxGSM (połączenie SSH)."""
        pass

    @lgsmset.command(name="host")
    async def lgsmset_host(self, ctx: commands.Context, host: str, port: int = 22):
        """
        Ustawia adres hosta z LinuxGSM oraz port SSH (domyślnie 22).

        Przykład: [p]lgsmset host 192.168.1.10 22
        """
        await self.config.guild(ctx.guild).ssh_host.set(host)
        await self.config.guild(ctx.guild).ssh_port.set(port)
        await ctx.send(f"✅ Host SSH ustawiony na: `{host}:{port}`")

    @lgsmset.command(name="user")
    async def lgsmset_user(self, ctx: commands.Context, ssh_user: str):
        """
        Ustawia użytkownika SSH (systemowego), na którym działa LinuxGSM.

        Przykład: [p]lgsmset user vhserver
        """
        await self.config.guild(ctx.guild).ssh_user.set(ssh_user)
        await ctx.send(f"✅ Użytkownik SSH ustawiony na: `{ssh_user}`")

    @lgsmset.command(name="path")
    async def lgsmset_path(self, ctx: commands.Context, *, path: str):
        """
        Ustawia ścieżkę (na hoście zdalnym) do skryptu serwera LinuxGSM.

        Przykład: [p]lgsmset path /home/vhserver/csgoserver/csgoserver
        """
        await self.config.guild(ctx.guild).script_path.set(path)
        await ctx.send(f"✅ Ścieżka do skryptu ustawiona na: `{path}`")

    @lgsmset.command(name="uploadkey")
    async def lgsmset_uploadkey(self, ctx: commands.Context):
        """
        Wgrywa prywatny klucz SSH (jako załącznik do wiadomości) używany do
        logowania na hosta z LinuxGSM.

        WAŻNE:
        - Wyślij tę komendę z załączonym plikiem klucza prywatnego (np. id_ed25519).
        - Klucz publiczny (id_ed25519.pub) musi być dodany do
          ~/.ssh/authorized_keys użytkownika ssh_user na zdalnym hoście.
        - Usuń wiadomość z załącznikiem po wysłaniu, jeśli kanał nie jest prywatny -
          klucz zostanie zapisany lokalnie, ale sama wiadomość na Discordzie
          może dalej zawierać załącznik w historii czatu.
        """
        if not ctx.message.attachments:
            await ctx.send("⚠️ Musisz załączyć plik klucza prywatnego do tej wiadomości.")
            return

        attachment = ctx.message.attachments[0]
        key_bytes = await attachment.read()

        key_name = f"{ctx.guild.id}_id_key"
        key_path = self._keys_dir() / key_name

        with open(key_path, "wb") as f:
            f.write(key_bytes)

        # Ustawiamy restrykcyjne uprawnienia pliku klucza (tylko właściciel może czytać/pisać)
        try:
            import os
            os.chmod(key_path, 0o600)
        except Exception:
            pass

        await self.config.guild(ctx.guild).ssh_key_file.set(key_name)
        await ctx.send(
            "✅ Klucz SSH zapisany i skonfigurowany.\n"
            "⚠️ Zalecam usunąć oryginalną wiadomość z załącznikiem dla bezpieczeństwa."
        )

    @lgsmset.command(name="addrole")
    async def lgsmset_addrole(self, ctx: commands.Context, role: discord.Role):
        """Dodaje rolę, która może zarządzać serwerem."""
        async with self.config.guild(ctx.guild).allowed_roles() as roles:
            if role.id not in roles:
                roles.append(role.id)
        await ctx.send(f"✅ Rola {role.mention} może teraz zarządzać serwerem.")

    @lgsmset.command(name="removerole")
    async def lgsmset_removerole(self, ctx: commands.Context, role: discord.Role):
        """Usuwa rolę z listy uprawnionych do zarządzania serwerem."""
        async with self.config.guild(ctx.guild).allowed_roles() as roles:
            if role.id in roles:
                roles.remove(role.id)
        await ctx.send(f"✅ Rola {role.mention} nie może już zarządzać serwerem.")

    @lgsmset.command(name="logchannel")
    async def lgsmset_logchannel(self, ctx: commands.Context, channel: discord.TextChannel = None):
        """Ustawia kanał logów akcji (lub wyłącza logowanie, jeśli podane bez kanału)."""
        if channel:
            await self.config.guild(ctx.guild).log_channel.set(channel.id)
            await ctx.send(f"✅ Logi będą wysyłane na {channel.mention}")
        else:
            await self.config.guild(ctx.guild).log_channel.set(None)
            await ctx.send("✅ Logowanie akcji wyłączone.")

    @lgsmset.command(name="testconnection")
    async def lgsmset_testconnection(self, ctx: commands.Context):
        """Testuje połączenie SSH z hostem (bez wykonywania akcji na serwerze gry)."""
        conf = await self.config.guild(ctx.guild).all()
        host, port, user, key_file = conf["ssh_host"], conf["ssh_port"], conf["ssh_user"], conf["ssh_key_file"]

        if not host:
            await ctx.send("⚠️ Host nie jest ustawiony.")
            return

        key_path = str(self._keys_dir() / key_file) if key_file else None
        connect_kwargs = {"host": host, "port": port, "username": user, "known_hosts": None}
        if key_path:
            connect_kwargs["client_keys"] = [key_path]

        try:
            async with ctx.typing():
                async with asyncssh.connect(**connect_kwargs) as conn:
                    result = await conn.run("whoami && echo OK", check=False)
            await ctx.send(f"✅ Połączono!\n```{result.stdout.strip()}```")
        except Exception as e:
            await ctx.send(f"❌ Nie udało się połączyć: `{e}`")

    @lgsmset.command(name="show")
    async def lgsmset_show(self, ctx: commands.Context):
        """Pokazuje bieżącą konfigurację."""
        conf = await self.config.guild(ctx.guild).all()
        roles = [ctx.guild.get_role(r) for r in conf["allowed_roles"]]
        roles = [r.mention for r in roles if r]
        channel = ctx.guild.get_channel(conf["log_channel"]) if conf["log_channel"] else None

        embed = discord.Embed(title="Konfiguracja LinuxGSM (SSH)", color=discord.Color.blue())
        embed.add_field(name="Host SSH", value=f"{conf['ssh_host']}:{conf['ssh_port']}" if conf["ssh_host"] else "Nie ustawiono", inline=False)
        embed.add_field(name="Użytkownik SSH", value=conf["ssh_user"] or "Nie ustawiono", inline=False)
        embed.add_field(name="Klucz SSH", value="Skonfigurowany ✅" if conf["ssh_key_file"] else "Brak ❌", inline=False)
        embed.add_field(name="Ścieżka skryptu", value=conf["script_path"] or "Nie ustawiono", inline=False)
        embed.add_field(name="Dozwolone role", value=", ".join(roles) if roles else "Tylko administratorzy", inline=False)
        embed.add_field(name="Kanał logów", value=channel.mention if channel else "Wyłączony", inline=False)
        await ctx.send(embed=embed)


async def setup(bot: Red):
    await bot.add_cog(LinuxGSM(bot))
