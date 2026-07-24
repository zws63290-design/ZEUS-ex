const {
    SlashCommandBuilder,
    ActionRowBuilder,
    StringSelectMenuBuilder,
    StringSelectMenuOptionBuilder,
    MessageFlags,
    EmbedBuilder
} = require('discord.js');

module.exports = {
    adminsOnly: true,

    data: new SlashCommandBuilder()
        .setName('to-select')
        .setDescription('تحويل التكت الى سلكت منيو')

        .addStringOption(option =>
            option
                .setName('message_id')
                .setDescription('ايدي الرسالة')
                .setRequired(true)
        )

        .addStringOption(option =>
            option.setName('description1')
                .setDescription('وصف الخيار الأول')
                .setRequired(false)
        )

        .addStringOption(option =>
            option.setName('description2')
                .setDescription('وصف الخيار الثاني')
                .setRequired(false)
        )

        .addStringOption(option =>
            option.setName('description3')
                .setDescription('وصف الخيار الثالث')
                .setRequired(false)
        )

        .addStringOption(option =>
            option.setName('description4')
                .setDescription('وصف الخيار الرابع')
                .setRequired(false)
        )

        .addStringOption(option =>
            option.setName('description5')
                .setDescription('وصف الخيار الخامس')
                .setRequired(false)
        )

        .addStringOption(option =>
            option.setName('emoji1')
                .setDescription('إيموجي الخيار الأول')
                .setRequired(false)
        )

        .addStringOption(option =>
            option.setName('emoji2')
                .setDescription('إيموجي الخيار الثاني')
                .setRequired(false)
        )

        .addStringOption(option =>
            option.setName('emoji3')
                .setDescription('إيموجي الخيار الثالث')
                .setRequired(false)
        )

        .addStringOption(option =>
            option.setName('emoji4')
                .setDescription('إيموجي الخيار الرابع')
                .setRequired(false)
        )

        .addStringOption(option =>
            option.setName('emoji5')
                .setDescription('إيموجي الخيار الخامس')
                .setRequired(false)
        ),

    async execute(interaction) {

        const messageId = interaction.options.getString('message_id');

        const descriptions = [
            interaction.options.getString('description1'),
            interaction.options.getString('description2'),
            interaction.options.getString('description3'),
            interaction.options.getString('description4'),
            interaction.options.getString('description5'),
        ];

        const emojis = [
            interaction.options.getString('emoji1'),
            interaction.options.getString('emoji2'),
            interaction.options.getString('emoji3'),
            interaction.options.getString('emoji4'),
            interaction.options.getString('emoji5'),
        ];

        try {
            // جلب الرسالة
            const message = await interaction.channel.messages.fetch(messageId);

            // البحث عن صف الأزرار
            const buttonRow = message.components.find(row =>
                row.components.some(component => component.type === 2)
            );

            if (!buttonRow) {
                return interaction.reply({
                    content: '{emoji:circlex} لا توجد أزرار في الرسالة.',
                    flags: MessageFlags.Ephemeral,
                });
            }

            // إنشاء السلكت منيو
            const selectMenu = new StringSelectMenuBuilder()
                .setCustomId('ticket_select')
                .setPlaceholder('اختر نوع التذكرة من هنا!');

            // تحويل الأزرار إلى خيارات
            buttonRow.components.forEach((button, index) => {
                const option = new StringSelectMenuOptionBuilder()
                    .setLabel(button.label)
                    .setValue(button.customId);

                // الايموجي: نعطي الأولوية للزر الأصلي، ثم الإيموجي المُمرر
                if (button.emoji) {
                    option.setEmoji(button.emoji);
                } else if (emojis[index]) {
                    option.setEmoji(emojis[index]);
                }

                // الوصف
                if (descriptions[index]) {
                    option.setDescription(descriptions[index]);
                }

                selectMenu.addOptions(option);
            });

            // زر الريسيت
            selectMenu.addOptions(
                new StringSelectMenuOptionBuilder()
                    .setLabel('إعادة تعيين')
                    .setValue('reset')
                    .setEmoji(require('../../utils/emojis').clock.toString())
            );

            // صف السلكت منيو
            const finalSelectRow = new ActionRowBuilder().addComponents(selectMenu);

            // تعديل الرسالة
            await message.edit({
                components: [finalSelectRow],
            });

            // الرد النهائي
            let replyMessage = `{emoji:circlecheck} تم تحويل الأزرار إلى قائمة خيارات.`;
            if (descriptions.some(d => d)) {
                replyMessage += `\n{emoji:message} تم إضافة الأوصاف بنجاح.`;
            }
            if (emojis.some(e => e)) {
                replyMessage += `\n{emoji:edit} تم إضافة الإيموجيات المخصصة.`;
            }

            await interaction.reply({
                content: replyMessage,
                flags: MessageFlags.Ephemeral,
            });

        } catch (error) {
            console.error(error);
            if (!interaction.replied && !interaction.deferred) {
                return interaction.reply({
                    content: '{emoji:circlex} حدث خطأ: تأكد من أنك تستخدم الأمر في نفس الروم الذي توجد فيه الرسالة، وحاول مرة أخرى.',
                    flags: MessageFlags.Ephemeral,
                });
            } else {
                return interaction.editReply({
                    content: '{emoji:circlex} حدث خطأ أو انتهت المهلة. حاول مرة أخرى.',
                    components: [],
                    flags: MessageFlags.Ephemeral,
                });
            }
        }
    }
};