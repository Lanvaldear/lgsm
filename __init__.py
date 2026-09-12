from .linuxgsm import LinuxGSM


async def setup(bot):
    await bot.add_cog(LinuxGSM(bot))
